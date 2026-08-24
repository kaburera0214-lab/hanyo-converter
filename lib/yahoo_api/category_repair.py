# -*- coding: utf-8 -*-
"""Yahooのプロダクトカテゴリ未設定商品を、安全な項目指定CSVで補修する。"""
import csv
import difflib
import io
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter

import requests

from . import client

PROD_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TEST_BASE = "https://test.circus.shopping.yahooapis.jp/ShoppingWebService/V1"
SEARCH_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
TIMEOUT = (10, 25)
LYP_MEMBER_RATE = 0.98
MIN_INTERVAL = 1.05
_last_request = {}


def _base():
    return TEST_BASE if client._secret("YAHOO_USE_TEST").lower() in ("true", "1", "yes") \
        else PROD_BASE


def _headers():
    return {"Authorization": f"Bearer {client.access_token()}"}


def _rate_limit(key):
    elapsed = time.monotonic() - _last_request.get(key, 0.0)
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request[key] = time.monotonic()


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _xml_messages(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [f"応答XMLを解釈できません: {text[:200]}"]
    messages = []
    for el in root.iter():
        if _strip_ns(el.tag).lower() in ("error", "message") and (el.text or "").strip():
            messages.append(el.text.strip())
    return messages


def get_item(code):
    """商品参照APIからカテゴリ推定に必要な現在情報だけを返す。"""
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("YAHOO_SELLER_ID が未設定です。")
    _rate_limit("getItem")
    res = requests.get(f"{_base()}/getItem", headers=_headers(),
                       params={"seller_id": seller, "item_code": code}, timeout=TIMEOUT)
    if res.status_code in (401, 403):
        raise client.YahooAuthError(f"Yahoo APIの認証に失敗しました（HTTP {res.status_code}）。")
    if res.status_code >= 400:
        raise client.YahooError(f"Yahoo商品参照に失敗しました（HTTP {res.status_code}）: {res.text[:500]}")
    try:
        root = ET.fromstring(res.text)
    except ET.ParseError as e:
        raise client.YahooError(f"Yahoo商品参照XMLを解釈できません: {e}") from e
    result = next((el for el in root.iter() if _strip_ns(el.tag) == "Result"), None)
    if result is None:
        raise client.YahooError(f"Yahooに商品 {code} が見つかりません。")
    values = {}
    for el in result.iter():
        key = _strip_ns(el.tag)
        value = (el.text or "").strip()
        if value and key not in values:
            values[key] = value
    return {"code": str(code), "name": values.get("Name", ""),
            "jan": values.get("Jan", ""),
            "product_category": values.get("ProductCategory", "")}


def _normalize_title(value):
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    value = re.sub(r"[\[\]【】()（）<>＜＞「」『』]", " ", value)
    value = re.sub(r"送料無料|送料込み|新品|正規品|公式", " ", value)
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠]+", "", value)


def _similarity(left, right):
    a, b = _normalize_title(left), _normalize_title(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _search_hits(item):
    appid = client._secret("YAHOO_CLIENT_ID")
    if not appid:
        raise client.YahooNotConfigured("YAHOO_CLIENT_ID が未設定です。")
    attempts = []
    if item.get("jan"):
        attempts.append(("JAN", {"jan_code": item["jan"]}))
    if item.get("name"):
        attempts.append(("商品名", {"query": item["name"][:120]}))
    for source, query in attempts:
        _rate_limit("itemSearch")
        params = {"appid": appid, "results": 20, **query}
        res = requests.get(SEARCH_URL, params=params, timeout=TIMEOUT)
        if res.status_code >= 400:
            continue
        try:
            hits = res.json().get("hits") or []
        except ValueError:
            hits = []
        usable = [h for h in hits if int((h.get("genreCategory") or {}).get("id") or 0) > 0]
        if usable:
            return source, usable
    return "", []


def _nearest_store_category(item, max_distance=3):
    """連番の近い店内商品から、商品名も似ているカテゴリ候補を探す。"""
    match = re.match(r"^(.*?)(\d+)$", str(item.get("code") or ""))
    if not match:
        return None
    prefix, number_text = match.groups()
    number = int(number_text)
    candidates = []
    for distance in range(1, max_distance + 1):
        for neighbor_number in (number - distance, number + distance):
            if neighbor_number < 0:
                continue
            neighbor_code = f"{prefix}{neighbor_number:0{len(number_text)}d}"
            try:
                neighbor = get_item(neighbor_code)
            except Exception:  # noqa: BLE001
                continue
            category_id = int(neighbor.get("product_category") or 0)
            if category_id <= 0:
                continue
            score = _similarity(item.get("name"), neighbor.get("name"))
            candidates.append((score, distance, neighbor, category_id))
        if candidates and max(row[0] for row in candidates) >= 0.55:
            break
    if not candidates:
        return None
    score, distance, neighbor, category_id = max(
        candidates, key=lambda row: (row[0], -row[1]))
    if score < 0.55:
        return None
    return {**item, "category_id": category_id, "category_name": "",
            "source": "Yahoo店内類似商品", "candidate_name": neighbor.get("name", ""),
            "score": round(float(score), 4),
            "reason": (f"店内類似商品 {neighbor['code']} のカテゴリを採用"
                       f"（商品名類似度 {score:.2f}）")}


def infer_product_category(code):
    """JANを優先し、無ければYahoo類似商品名からカテゴリを推定する。"""
    item = get_item(code)
    existing = int(item.get("product_category") or 0)
    if existing > 0:
        return {**item, "category_id": existing, "category_name": "",
                "source": "Yahoo既存値", "candidate_name": "", "score": 1.0,
                "reason": "既にプロダクトカテゴリが設定済み"}
    store_candidate = _nearest_store_category(item)
    if store_candidate:
        return store_candidate
    source, hits = _search_hits(item)
    if not hits:
        raise client.YahooError("Yahoo商品検索でカテゴリ付きの類似商品が見つかりません。")

    if source == "JAN":
        exact = [h for h in hits if str(h.get("janCode") or "") == str(item.get("jan") or "")]
        pool = exact or hits
        counts = Counter(int(h["genreCategory"]["id"]) for h in pool)
        category_id, votes = counts.most_common(1)[0]
        candidate = next(h for h in pool if int(h["genreCategory"]["id"]) == category_id)
        score = votes / len(pool)
        reason = f"Yahoo商品検索の同一JAN {item['jan']}（{votes}/{len(pool)}件が同カテゴリ）"
    else:
        ranked = sorted(((_similarity(item["name"], h.get("name")), h) for h in hits),
                        key=lambda pair: pair[0], reverse=True)
        score, candidate = ranked[0]
        category_id = int(candidate["genreCategory"]["id"])
        reason = f"Yahoo商品検索の商品名類似度 {score:.2f}"

    genre = candidate.get("genreCategory") or {}
    return {**item, "category_id": category_id,
            "category_name": str(genre.get("name") or ""), "source": source,
            "candidate_name": str(candidate.get("name") or ""),
            "score": round(float(score), 4), "reason": reason}


def plan_categories(codes, on_progress=None):
    plans, failures = {}, {}
    unique = list(dict.fromkeys(str(c).strip().lower() for c in codes if str(c).strip()))
    for index, code in enumerate(unique, start=1):
        try:
            plans[code] = infer_product_category(code)
        except Exception as e:  # noqa: BLE001
            failures[code] = str(e)
        if on_progress:
            on_progress(index, len(unique), f"カテゴリ推定 {index}/{len(unique)}: {code}")
    return plans, failures


def _category_price_csv(plans, price_by_code):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[
        "code", "product-category", "price", "sale-price", "member-price"],
        lineterminator="\r\n")
    writer.writeheader()
    for code, plan in plans.items():
        price = int(price_by_code[code])
        writer.writerow({"code": code, "product-category": int(plan["category_id"]),
                         "price": price, "sale-price": "",
                         "member-price": int(round(price * LYP_MEMBER_RATE))})
    return stream.getvalue().encode("cp932")


def upload_category_prices(plans, price_by_code):
    """項目指定(type=4)でカテゴリと価格だけを更新し、他の商品項目を保持する。"""
    if not plans:
        return []
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("YAHOO_SELLER_ID が未設定です。")
    payload = _category_price_csv(plans, price_by_code)
    _rate_limit("uploadItemFile")
    res = requests.post(f"{_base()}/uploadItemFile", params={"seller_id": seller},
                        headers=_headers(), data={"type": "4"},
                        files={"file": ("category_price.csv", payload, "text/csv")},
                        timeout=TIMEOUT)
    if res.status_code in (401, 403):
        raise client.YahooAuthError(f"Yahoo APIの認証に失敗しました（HTTP {res.status_code}）。")
    if res.status_code >= 400:
        return [f"YahooカテゴリCSV更新 HTTP {res.status_code}: {res.text[:1000]}"]
    return _xml_messages(res.text)


def repair_category_prices(price_by_code, plans=None, on_progress=None):
    """カテゴリを推定し、推定できた商品だけカテゴリ＋価格を項目指定更新する。"""
    normalized = {str(k).strip().lower(): int(v) for k, v in price_by_code.items()}
    if plans is None:
        plans, failures = plan_categories(normalized.keys(), on_progress=on_progress)
    else:
        plans = {c: p for c, p in plans.items() if c in normalized and p.get("category_id")}
        failures = {c: "保存済みカテゴリ候補がありません" for c in normalized if c not in plans}
    errors = upload_category_prices(plans, normalized)
    if errors:
        return {}, {code: "／".join(errors[:5]) for code in normalized}
    return plans, failures
