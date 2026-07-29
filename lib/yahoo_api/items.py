# -*- coding: utf-8 -*-
"""
Yahoo!ショッピング 商品一括更新API（updateItems）＋ 全反映予約API（reservePublish）。

- updateItems: 商品データを部分更新（価格 price 等）。1回最大100件。
    POST {BASE}/ShoppingWebService/V1/updateItems
    ヘッダ: Authorization: Bearer <access_token>
    パラメータ: appid=<client_id>, seller_id=<ストアアカウント>,
              item1..item100 = "item_code=xxx&price=yyy" をRFC3986でpercent-encodeした文字列
- reservePublish: 更新は自動で店頭反映されないため、更新後に全反映予約を1回呼ぶ。
    POST {BASE}/ShoppingWebService/V1/reservePublish （seller_id）

テスト環境は BASE を test.circus に切り替える（Secrets YAHOO_USE_TEST=true）。
レスポンスはXML（ShoppingWebService）。成否とエラーメッセージを取り出す。
"""
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from . import client

PROD_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TEST_BASE = "https://test.circus.shopping.yahooapis.jp/ShoppingWebService/V1"
# (接続, 読み取り)秒。長時間ハングでスクリプトごと打ち切られるのを防ぐため短め。
TIMEOUT = (10, 20)
MAX_ITEMS = 100


def _base():
    return TEST_BASE if client._secret("YAHOO_USE_TEST").lower() in ("true", "1", "yes") \
        else PROD_BASE


def _headers():
    return {"Authorization": f"Bearer {client.access_token()}"}


def _post(path, data):
    res = requests.post(f"{_base()}{path}", headers=_headers(), data=data, timeout=TIMEOUT)
    if res.status_code in (401, 403):
        raise client.YahooAuthError(
            f"Yahoo APIの認証に失敗しました（HTTP {res.status_code}）。再認可してください。")
    if res.status_code >= 400:
        raise client.YahooError(f"Yahoo APIエラー HTTP {res.status_code}: {res.text[:500]}")
    return res.text


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _errors_from_xml(text):
    """レスポンスXMLからエラーメッセージを抽出する（無ければ空list）。"""
    msgs = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [f"応答XMLを解釈できません: {text[:200]}"]
    for el in root.iter():
        if _strip_ns(el.tag).lower() in ("error", "message") and (el.text or "").strip():
            msgs.append(el.text.strip())
    return msgs


def update_prices(price_by_code):
    """{商品コード: 価格} を updateItems で部分更新する。
    返り値: (成功件数, エラーlist)。price以外は触らない（部分更新）。"""
    items = [(str(code), int(price)) for code, price in price_by_code.items()
             if str(code).strip() and price]
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID（ストアアカウント）が未設定です。")
    ok, errors = 0, []
    for i in range(0, len(items), MAX_ITEMS):
        chunk = items[i:i + MAX_ITEMS]
        data = {"appid": client._secret("YAHOO_CLIENT_ID"), "seller_id": seller}
        for n, (code, price) in enumerate(chunk, start=1):
            # 1商品 = "item_code=xxx&price=yyy" をpercent-encodeして itemN に入れる
            data[f"item{n}"] = urllib.parse.quote(f"item_code={code}&price={price}", safe="")
        text = _post("/updateItems", data)
        errs = _errors_from_xml(text)
        if errs:
            errors.extend(errs)
        else:
            ok += len(chunk)
    return ok, errors


def reserve_publish():
    """全反映予約（更新内容を店頭へ反映する）。更新後に1回呼ぶ。"""
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID が未設定です。")
    text = _post("/reservePublish",
                 {"appid": client._secret("YAHOO_CLIENT_ID"), "seller_id": seller})
    return _errors_from_xml(text)


def test_connection():
    """テスト環境の疎通確認（アクセストークンが有効かを軽く確認する）。"""
    return client.access_token()[:6] + "…"
