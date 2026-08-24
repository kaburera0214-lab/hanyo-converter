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

from . import category_repair, client

PROD_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TEST_BASE = "https://test.circus.shopping.yahooapis.jp/ShoppingWebService/V1"
# (接続, 読み取り)秒。長時間ハングでスクリプトごと打ち切られるのを防ぐため短め。
TIMEOUT = (10, 20)
MAX_ITEMS = 100
# LYPプレミアム会員向け販売価格 = 通常販売価格の2%引き（ユーザー確定 2026-07-30）。
LYP_MEMBER_RATE = 0.98


class YahooHTTPError(client.YahooError):
    """Yahoo APIのHTTPエラー。呼び出し側でXMLのエラー内容を判定できるよう本文を保持する。"""

    def __init__(self, status_code, body):
        self.status_code = int(status_code)
        self.body = str(body or "")
        super().__init__(f"Yahoo APIエラー HTTP {self.status_code}: {self.body[:1500]}")


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
        raise YahooHTTPError(res.status_code, res.text)
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


def _not_found_positions(text):
    """updateItems応答から it-02002（商品なし）の itemN を返す。

    updateItemsは1件でも不正だと同じリクエスト内の全商品を更新しないため、商品なしだけが
    原因のときに限り、その商品を除いて安全に再送する。別種のエラーが混在する場合は
    原因を隠さないよう空listを返し、通常の失敗として扱う。
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows = []
    for res in root.iter():
        if _strip_ns(res.tag) != "Result":
            continue
        values = {}
        for el in res.iter():
            key = _strip_ns(el.tag)
            value = (el.text or "").strip()
            if value:
                values[key] = value
        if values.get("Code"):
            rows.append(values)
    if not rows or any(r.get("Code") != "it-02002" for r in rows):
        return []
    positions = []
    for row in rows:
        key = row.get("ErrorKey", "")
        if not key.startswith("item") or not key[4:].isdigit():
            return []
        positions.append(int(key[4:]))
    return sorted(set(positions))


def _result_error_rows(text):
    """updateItems応答のエラー行を [{Code, ErrorKey, Message, ...}] で返す。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows = []
    for res in root.iter():
        if _strip_ns(res.tag) != "Result":
            continue
        values = {}
        for el in res.iter():
            key = _strip_ns(el.tag)
            value = (el.text or "").strip()
            if value:
                values[key] = value
        if values.get("Code"):
            rows.append(values)
    return rows


def _repairable_positions(text):
    """商品なし(it-02002)とカテゴリ未設定(it-02037)だけなら位置を分類する。"""
    rows = _result_error_rows(text)
    if not rows or any(row.get("Code") not in ("it-02002", "it-02037") for row in rows):
        return None
    found = {"missing": [], "category": []}
    for row in rows:
        key = row.get("ErrorKey", "")
        if not key.startswith("item") or not key[4:].isdigit():
            return None
        group = "missing" if row["Code"] == "it-02002" else "category"
        found[group].append(int(key[4:]))
    return {key: sorted(set(value)) for key, value in found.items()}


def _update_data(seller, chunk):
    """updateItems 1リクエスト分のPOSTデータを作る。"""
    data = {"seller_id": seller}
    for n, (code, price) in enumerate(chunk, start=1):
        member = int(round(price * LYP_MEMBER_RATE))
        data[f"item{n}"] = (f"item_code={code}&price={price}"
                            f"&sale_price=&member_price={member}")
    return data


def update_prices_checked(price_by_code, category_plans=None, on_category_repair=None):
    """Yahoo価格を更新し、商品未登録だけを除外して残りを再送する。

    返り値: (成功件数, エラーlist, Yahooに存在しなかった商品コードlist)。
    it-02002以外のエラーは除外・再送せず、そのままエラーとして返す。
    """
    items = [(str(code), int(price)) for code, price in price_by_code.items()
             if str(code).strip() and price]
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID（ストアアカウント）が未設定です。")
    ok, errors, missing = 0, [], []
    for i in range(0, len(items), MAX_ITEMS):
        pending = items[i:i + MAX_ITEMS]
        while pending:
            http_error = None
            try:
                text = _post("/updateItems", _update_data(seller, pending))
            except YahooHTTPError as e:
                http_error = e
                text = e.body

            classified = _repairable_positions(text)
            if classified is not None:
                all_positions = classified["missing"] + classified["category"]
                if any(p < 1 or p > len(pending) for p in all_positions):
                    errors.append(str(http_error) if http_error else "Yahoo応答の商品位置が不正です。")
                    break

                missing.extend(pending[p - 1][0] for p in classified["missing"])
                category_prices = {pending[p - 1][0]: pending[p - 1][1]
                                   for p in classified["category"]}
                if category_prices:
                    saved_plans = None
                    if category_plans is not None:
                        saved_plans = {code: category_plans[code] for code in category_prices
                                       if code in category_plans}
                    repaired, repair_failures = category_repair.repair_category_prices(
                        category_prices, plans=saved_plans)
                    ok += len(repaired)
                    if on_category_repair:
                        for code, detail in repaired.items():
                            on_category_repair(code, detail)
                    errors.extend(f"{code}: {message}" for code, message in repair_failures.items())

                omitted = set(all_positions)
                pending = [item for n, item in enumerate(pending, start=1) if n not in omitted]
                if pending:
                    continue
                break

            if http_error is not None:
                errors.append(str(http_error))
                break
            errs = _errors_from_xml(text)
            if errs:
                errors.extend(errs)
            else:
                ok += len(pending)
            break
        if errors:
            break
    return ok, errors, missing


def update_prices(price_by_code):
    """{商品コード: 価格} を updateItems で更新する（親コード＝Yahoo商品コード単位）。
    返り値: (成功件数, エラーlist)。設定するのは以下（ユーザー確定 2026-07-30・公式仕様準拠）:
      - price（通常販売価格）= 指定価格
      - sale_price（セール価格）= 空文字。公式に「利用しない場合は空文字を指定」と明記。
        price 更新時に sale_price 項目自体は必須（未指定は it-02022 で400）だが、空文字で
        「セールなし」にできる。※既存の特価があればこの更新でクリアされる（割引なしに揃う）。
      - member_price（LYPプレミアム会員向け販売価格）= priceの2%引き。
    値制約: member_price<price(it-02026)を満たす。sale_priceが空なので
    sale_price<price(it-02011)・member_price<sale_price(it-02027)は無関係。"""
    ok, errors, missing = update_prices_checked(price_by_code)
    errors.extend(f"{code}: 指定された商品は存在しません（it-02002）" for code in missing)
    return ok, errors


def reserve_publish():
    """全反映予約（更新内容を店頭へ反映する）。更新後に1回呼ぶ。
    mode=1（反映予約・予約日時変更）が必須。reserve_time を省略すると現在時刻で予約
    ＝実質即時反映（未指定は pm-05005「modeが指定されていません」で400）。"""
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID が未設定です。")
    # 認証は Authorization: Bearer のみ（公式仕様）。appid は本文に入れない。
    text = _post("/reservePublish", {"seller_id": seller, "mode": 1})
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
