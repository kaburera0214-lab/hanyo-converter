# -*- coding: utf-8 -*-
"""
Yahoo!ショッピングのカテゴリマスタから、**実在するプロダクトカテゴリID**を取る。

  GET {BASE}/getShopCategoryList?query=..   … Yahoo全体のカテゴリをキーワード検索
  GET {BASE}/getShopCategory?category_code=.. … そのカテゴリの子（末端まで降りる）

⚠️ ここが今回の詰まりどころ。**SHPカテゴリコードとプロダクトカテゴリIDは別物**。
getShopCategory の CategoryCode は、
  IsLeaf=0 … SHPカテゴリコード（まだ途中。ここを product-category に書いても無効）
  IsLeaf=1 … **プロダクトカテゴリID**（これだけが product-category に書ける）
検索APIが返すのもSHPカテゴリコードなので、**末端まで降りないと使えるIDにならない**。
2026-09-10: これを取り違えて無効なIDを送り続け、U-001-0363 が消えなかった。

方針（2026-09-10 ユーザー確定）:
- 店内の類似商品からIDをコピーする方式はリスクが高いので使わない
- Yahoo全体を、語を減らしながら段階的に広く検索する
  「フェルト ワンピース イエロー」→「フェルト ワンピース」→「フェルト」
- 厳密さより**まず処理が通ること**を優先し、必ず末端まで降りて有効なIDを返す
"""
import xml.etree.ElementTree as ET

import requests

from . import client

PROD_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TEST_BASE = "https://test.circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TIMEOUT = (10, 30)
_MIN_INTERVAL = 1.1          # 公式の上限が 1クエリー/秒
_last_call = [0.0]
MAX_DEPTH = 8                # ツリーを降りる上限（無限ループ防止）


def _base():
    return TEST_BASE if client._secret("YAHOO_USE_TEST").lower() in ("true", "1", "yes") \
        else PROD_BASE


def _rate_limit():
    import time
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _get(path, params):
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID が未設定です。")
    _rate_limit()
    res = requests.get(f"{_base()}{path}",
                       headers={"Authorization": f"Bearer {client.access_token()}"},
                       params={"seller_id": seller, **params}, timeout=TIMEOUT)
    if res.status_code in (401, 403):
        raise client.YahooAuthError(
            f"Yahoo APIの認証に失敗しました（HTTP {res.status_code}）。再認可してください。")
    body = client.decode(res)
    if res.status_code >= 400:
        raise client.YahooError(f"{path} に失敗しました（HTTP {res.status_code}）: {body[:400]}")
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise client.YahooError(f"{path} の応答XMLを解釈できません: {e}／{body[:300]}") from e


def _rows(root):
    out = []
    for result in root.iter():
        if _strip_ns(result.tag) != "Result":
            continue
        out.append({_strip_ns(child.tag): (child.text or "").strip() for child in result})
    return out


def children(category_code=None):
    """カテゴリの子。category_code 省略で第1階層。 [{code, name, is_leaf}]"""
    params = {} if category_code in (None, "") else {"category_code": int(category_code)}
    return [{"code": r.get("CategoryCode", ""), "name": r.get("CategoryName", ""),
             "is_leaf": str(r.get("IsLeaf", "")).strip() == "1"}
            for r in _rows(_get("/getShopCategory", params))
            if str(r.get("CategoryCode", "")).strip()]


def search(query, results=25):
    """Yahoo全体のカテゴリをキーワード検索。 [{code, name, path}]"""
    rows = _rows(_get("/getShopCategoryList",
                      {"query": str(query)[:100], "results": int(results)}))
    return [{"code": r.get("CategoryCode", ""), "name": r.get("CategoryName", ""),
             "path": r.get("PathName", "")} for r in rows
            if str(r.get("CategoryCode", "")).strip()]


def descend_to_leaf(category_code, max_depth=MAX_DEPTH):
    """カテゴリコードから末端まで降り、**プロダクトカテゴリID**を返す。

    返り値 {"category_id", "category_name", "path"} / 見つからなければ None。
    途中で分岐したら先頭の子を選ぶ（厳密さより「通ること」を優先する方針）。
    """
    code, names = category_code, []
    for _ in range(max_depth):
        try:
            kids = children(code)
        except client.YahooError:
            return None
        if not kids:
            return None
        leaf = next((k for k in kids if k["is_leaf"]), None)
        if leaf:
            names.append(leaf["name"])
            return {"category_id": int(leaf["code"]),
                    "category_name": leaf["name"], "path": " > ".join(names)}
        code = kids[0]["code"]
        names.append(kids[0]["name"])
    return None


def _query_variants(name):
    """「フェルト ワンピース イエロー」→「フェルト ワンピース」→「フェルト」。

    語を減らしながら広げる。狭いまま0件で諦めると、カテゴリを付けられない。
    """
    words = [w for w in str(name or "").replace("　", " ").split(" ") if w]
    seen, out = set(), []
    for length in range(min(3, len(words)), 0, -1):
        query = " ".join(words[:length])
        if query and query not in seen:
            seen.add(query)
            out.append(query)
    return out


def resolve_from_name(name, on_step=None):
    """商品名から、**実在するプロダクトカテゴリID**を決める。

    返り値 {"category_id", "category_name", "path", "query"} / 決められなければ None。
    語を減らしながら検索し、当たったカテゴリを末端まで降りてIDにする。
    """
    for query in _query_variants(name):
        if on_step:
            on_step(f"カテゴリ検索: 「{query}」")
        try:
            hits = search(query, results=10)
        except client.YahooError:
            continue
        for hit in hits:
            leaf = descend_to_leaf(hit["code"])
            if leaf:
                return {**leaf, "query": query,
                        "path": hit.get("path") or leaf.get("path") or hit["name"]}
    return None


def any_valid_category(on_step=None):
    """最後の手段: ツリーの先頭から降りて、確実に実在するIDを1つ取る。

    厳密なカテゴリより「まず処理を通す」ことを優先する方針（2026-09-10 確定）。
    どのカテゴリを入れたかは必ず画面に出すこと（あとで人が直せるように）。
    """
    if on_step:
        on_step("カテゴリ検索: 既定のカテゴリを取得中…")
    try:
        top = children()
    except client.YahooError:
        return None
    for node in top:
        leaf = descend_to_leaf(node["code"])
        if leaf:
            return {**leaf, "query": "（既定）"}
    return None
