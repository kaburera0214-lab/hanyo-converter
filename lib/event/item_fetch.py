# -*- coding: utf-8 -*-
"""
商品管理番号から商品情報(名前・価格・画像・レビュー)を取得する。

- 商品名・価格: RMS Item API 2.0 (自店の正データ。非公開商品も取得可)
- レビュー件数・平均点・画像: 楽天市場商品検索API(公開API、RAKUTEN_APP_ID)
RMSキー未設定/期限切れでも検索API単独で動作継続する(公開中商品のみ・警告表示)。
"""
import re

import requests

from . import rms_api

SEARCH_URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
TIMEOUT = 20


def shop_code():
    import streamlit as st
    return str(st.secrets.get("GOLD_SHOP_URL", "")).strip()


def item_url(manage_number):
    return f"https://item.rakuten.co.jp/{shop_code()}/{manage_number}/"


def search_configured():
    import streamlit as st
    return bool(st.secrets.get("RAKUTEN_APP_ID", ""))


# ---- RMS Item API 2.0 ----
def _fetch_rms(manage_number):
    """RMS Item API 2.0 から {name, price, image_url} を返す(取れない項目はNone)。"""
    data = rms_api.get(f"/es/2.0/items/manage-numbers/{manage_number}")
    item = data.get("item", data)
    name = item.get("title") or None
    # 価格はSKU(variants)ごと。最安値を採用する
    prices = []
    variants = item.get("variants") or {}
    if isinstance(variants, dict):
        variants = variants.values()
    for v in variants:
        if not isinstance(v, dict):
            continue
        for key in ("standardPrice", "price", "salesPrice"):
            p = v.get(key)
            if p is not None:
                try:
                    prices.append(int(float(p)))
                except (TypeError, ValueError):
                    pass
                break
    price = min(prices) if prices else None
    image_url = None
    images = item.get("images") or []
    if images and isinstance(images[0], dict):
        loc = images[0].get("location") or ""
        typ = str(images[0].get("type") or "").upper()
        if loc.startswith("http"):
            image_url = loc
        elif loc and typ == "GOLD":
            # GOLD領域の画像(例 /gold/LP/xxx.jpg → https://shop.r10s.jp/gold/{shop}/gold/LP/xxx.jpg)
            image_url = f"https://shop.r10s.jp/gold/{shop_code()}{loc}"
        elif loc:
            image_url = f"https://image.rakuten.co.jp/{shop_code()}/cabinet{loc}"
    return {"name": name, "price": price, "image_url": image_url}


# ---- 楽天市場商品検索API(公開) ----
def _fetch_search(manage_number):
    """検索APIから {name, price, image_url, review_count, review_average} を返す。"""
    import streamlit as st
    app_id = str(st.secrets.get("RAKUTEN_APP_ID", "")).strip()
    if not app_id:
        return None
    params = {
        "applicationId": app_id,
        "itemCode": f"{shop_code()}:{manage_number}".lower(),
        "hits": 1,
    }
    resp = requests.get(SEARCH_URL, params=params, timeout=TIMEOUT)
    if resp.status_code == 404:
        return None  # 該当商品なし(検索インデックス未反映等)
    if not resp.ok:
        # アプリID無効等はUIに出す(呼び出し側がwarningsに積む)
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    items = (resp.json() or {}).get("Items") or []
    if not items:
        return None
    it = items[0].get("Item", items[0])
    image_url = None
    for entry in it.get("mediumImageUrls") or []:
        u = entry.get("imageUrl") if isinstance(entry, dict) else entry
        if u:
            image_url = re.sub(r"\?_ex=\d+x\d+$", "", u)  # サムネ指定を外して原寸に
            break
    return {
        "name": it.get("itemName"),
        "price": it.get("itemPrice"),
        "image_url": image_url,
        "review_count": it.get("reviewCount") or 0,
        "review_average": it.get("reviewAverage") or 0,
    }


def fetch_items(manage_numbers):
    """
    商品管理番号リストから商品情報を一括取得する。
    戻り値: (items: {管理番号: info}, errors: {管理番号: メッセージ}, warnings: [str])
    info = {manage_number, name, price, image_url, review_count, review_average, url}
    """
    items, errors, warnings = {}, {}, []
    rms_dead = not rms_api.is_configured()
    if rms_dead:
        warnings.append("RMSキー未設定のため検索API(公開中商品のみ)で取得します。")
    if not search_configured():
        warnings.append("RAKUTEN_APP_ID 未設定のためレビュー情報は取得できません。")
    for mn in manage_numbers:
        mn = str(mn).strip()
        if not mn or mn in items:
            continue
        rms, search = None, None
        if not rms_dead:
            try:
                rms = _fetch_rms(mn)
            except rms_api.RMSAuthError as e:
                rms_dead = True  # 以降の商品でも無駄打ちしない
                warnings.append(str(e))
            except rms_api.RMSError as e:
                errors[mn] = f"RMS: {e}"
        try:
            search = _fetch_search(mn)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{mn}: 検索APIエラー({e})")
        if not rms and not search:
            errors.setdefault(mn, "商品情報を取得できませんでした(管理番号の誤り、または非公開商品でRMSキー未設定)")
            continue
        rms = rms or {}
        search = search or {}
        name = rms.get("name") or search.get("name") or mn
        items[mn] = {
            "manage_number": mn,
            "name": name,
            "price": rms.get("price") if rms.get("price") is not None else search.get("price"),
            "image_url": search.get("image_url") or rms.get("image_url") or "",
            "review_count": search.get("review_count") or 0,
            "review_average": search.get("review_average") or 0,
            "url": item_url(mn),
        }
        errors.pop(mn, None)
    return items, errors, warnings


def parse_manage_numbers(text):
    """改行・カンマ・空白区切りの入力文字列から管理番号リストを返す(順序保持・重複除去)。"""
    seen, out = set(), []
    for token in re.split(r"[\s,、]+", str(text or "")):
        token = token.strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out
