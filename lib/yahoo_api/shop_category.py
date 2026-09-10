# -*- coding: utf-8 -*-
"""
Yahoo!ショッピング SHPカテゴリ（＝プロダクトカテゴリ）のマスタ参照。

  GET {BASE}/getShopCategory?seller_id=..&category_code=..   … ツリー（子を返す）
  GET {BASE}/getShopCategoryList?seller_id=..&query=..       … キーワード検索

なぜ要るか: プロダクトカテゴリIDは**Yahooのマスタに実在するものしか使えない**。
店内の類似商品からIDをコピーする方式（category_repair）は、コピー元のIDが古くて
使えなくなっていると、そのまま無効なIDを送ってしまう。実際 artc4168 で
`U-001-0363 プロダクトカテゴリが存在しません` が消えなかった原因がこれ。

**推定したIDは、送る前にここで実在を確かめる。**確かめられないIDは送らない
（送っても行ごと弾かれるだけで、原因も分からなくなる）。

getShopCategory は IsLeaf=1 のとき CategoryCode が**プロダクトカテゴリID**になる。
"""
import xml.etree.ElementTree as ET

import requests

from . import client

PROD_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TEST_BASE = "https://test.circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TIMEOUT = (10, 30)
_MIN_INTERVAL = 1.1          # 公式の上限が 1クエリー/秒
_last_call = [0.0]


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
    """カテゴリの子を返す。category_code 省略で第1階層。

    [{code, name, is_leaf}] （is_leaf=True の code がプロダクトカテゴリID）
    """
    params = {} if category_code in (None, "") else {"category_code": int(category_code)}
    return [{"code": r.get("CategoryCode", ""), "name": r.get("CategoryName", ""),
             "is_leaf": str(r.get("IsLeaf", "")).strip() == "1"}
            for r in _rows(_get("/getShopCategory", params))]


def search(query, results=25):
    """キーワードでカテゴリを検索する。 [{code, name, path}]"""
    rows = _rows(_get("/getShopCategoryList",
                      {"query": str(query)[:100], "results": int(results)}))
    return [{"code": r.get("CategoryCode", ""), "name": r.get("CategoryName", ""),
             "path": r.get("PathName", "")} for r in rows]


def exists(category_id):
    """そのプロダクトカテゴリIDがYahooのマスタに実在するか。

    判定できなかった場合は None を返す（「無い」と断定しない）。
    実在しないIDを送ると U-001-0363 で行ごと弾かれるので、送る前に必ず確かめる。
    """
    try:
        code = int(str(category_id).strip() or 0)
    except ValueError:
        return False
    if code <= 0:
        return False
    try:
        # 末端（プロダクトカテゴリ）なら子は返らない。存在しないIDはエラーになる。
        children(code)
        return True
    except client.YahooAuthError:
        raise
    except client.YahooError as e:
        message = str(e)
        # 「存在しない」と明確に言われた場合だけ False。それ以外は判定不能。
        if "存在" in message or "不正" in message or "HTTP 400" in message:
            return False
        return None


def find_by_name(name, limit=5):
    """商品名からプロダクトカテゴリ候補を探す。 [{code, name, path}]（先頭が最有力）"""
    words = [w for w in str(name or "").replace("　", " ").split(" ") if w]
    tried = []
    for length in (3, 2, 1):
        query = " ".join(words[:length])
        if not query or query in tried:
            continue
        tried.append(query)
        try:
            hits = search(query, results=limit)
        except client.YahooError:
            continue
        if hits:
            return hits
    return []
