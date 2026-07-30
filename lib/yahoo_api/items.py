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
        raise client.YahooError(f"Yahoo APIエラー HTTP {res.status_code}: {res.text[:1500]}")
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
    """{商品コード: 価格} を updateItems で更新する（親コード＝Yahoo商品コード単位）。
    返り値: (成功件数, エラーlist)。
    Yahooのpartial updateは price 更新時に sale_price も必須（未指定は it-02022 で400）。
    価格改定は「通常の販売価格を設定」する用途なので price=sale_price（割引なし）で送る
    ＝NE売価・楽天standardPriceと同じ意味。既存の特価があれば通常価格に揃う点に注意。"""
    items = [(str(code), int(price)) for code, price in price_by_code.items()
             if str(code).strip() and price]
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID（ストアアカウント）が未設定です。")
    ok, errors = 0, []
    for i in range(0, len(items), MAX_ITEMS):
        chunk = items[i:i + MAX_ITEMS]
        # 認証は Authorization: Bearer のみ（公式仕様）。appid は本文に入れない。
        data = {"seller_id": seller}
        for n, (code, price) in enumerate(chunk, start=1):
            # 1商品 = "item_code=xxx&price=yyy&sale_price=yyy"。requestsが1回
            # percent-encodeするのでここでは生の文字列（自前quoteは二重encodeで壊れる）。
            data[f"item{n}"] = f"item_code={code}&price={price}&sale_price={price}"
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
    # 認証は Authorization: Bearer のみ（公式仕様）。appid は本文に入れない。
    text = _post("/reservePublish", {"seller_id": seller})
    return _errors_from_xml(text)


def get_stock(codes):
    """在庫参照API(getStock)で商品を“読むだけ”実行する（切り分け用・書き込みなし）。
    codes: 商品コードのlist。個別商品コードは "商品コード:個別コード"（コロン）で渡す。
    返り値: (raw_xml, results, errors)。
      raw_xml … Yahooからの応答本文そのまま（画面表示・原因特定用）
      results … [{item_code, sub_code, quantity, is_published, ...}]（読めた分）
      errors  … XML内のError/Messageやパース不能時の文言list
    getStockはBearer認証のみ・seller_id＋item_code（カンマで最大1000件）。"""
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID（ストアアカウント）が未設定です。")
    item_code = ",".join(str(c).strip() for c in codes if str(c).strip())
    if not item_code:
        raise client.YahooError("参照する商品コードが空です。")
    text = _post("/getStock", {"seller_id": seller, "item_code": item_code})
    results, errors = [], _errors_from_xml(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text, results, errors or [f"応答XMLを解釈できません: {text[:200]}"]
    for res in root.iter():
        if _strip_ns(res.tag) != "Result":
            continue
        row = {_strip_ns(ch.tag): (ch.text or "").strip() for ch in res}
        if row:
            results.append(row)
    return text, results, errors


def test_connection():
    """テスト環境の疎通確認（アクセストークンが有効かを軽く確認する）。"""
    return client.access_token()[:6] + "…"
