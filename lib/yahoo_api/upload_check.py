# -*- coding: utf-8 -*-
"""
Yahoo!ショッピング データチェック履歴API（アップロードの合否をYahooから読む）。

  GET {BASE}/dataCheckHistorySummary?seller_id=..&file_type=2   … 商品アップロードの一覧
  GET {BASE}/dataCheckHistoryDetail?seller_id=..&check_id=..    … 行ごとのエラー

なぜ要るか: uploadItemFile は**ファイルを受け付けた時点で Status OK を返す**。
行ごとの検証はそのあと非同期で走るので、「受付OK」は「反映された」を意味しない。
反映されない理由（プロダクトカテゴリが存在しない、必須項目がない等）は
このAPIにしか出てこない。ここを読まないと、原因を人が画面で探すしかなくなる。

2026-09-10: artc4168 が反映されない原因を、ブラウザの画面を見に行ったり
推測したりで何往復もしてしまった。Yahoo自身の判定を読めば1回で分かる。
"""
import xml.etree.ElementTree as ET

import requests

from . import client

PROD_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TEST_BASE = "https://test.circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TIMEOUT = (10, 30)
FILE_TYPE_ITEM = 2          # 商品（file_type の値）
ERROR_TYPE_ERROR_ONLY = 1   # 1=エラーのみ（2=警告）

# CheckStatus: 0-3, 8-10。公式に完了/エラーの意味付けがあるが、判定には
# ErrorNum を使う（ステータスの意味を推測しないため）。


def _base():
    return TEST_BASE if client._secret("YAHOO_USE_TEST").lower() in ("true", "1", "yes") \
        else PROD_BASE


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _get(path, params):
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID が未設定です。")
    res = requests.get(f"{_base()}{path}",
                       headers={"Authorization": f"Bearer {client.access_token()}"},
                       params={"seller_id": seller, **params}, timeout=TIMEOUT)
    if res.status_code in (401, 403):
        raise client.YahooAuthError(
            f"Yahoo APIの認証に失敗しました（HTTP {res.status_code}）。再認可してください。")
    body = client.decode(res)
    if res.status_code >= 400:
        raise client.YahooError(f"{path} に失敗しました（HTTP {res.status_code}）: {body[:500]}")
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise client.YahooError(f"{path} の応答XMLを解釈できません: {e}／{body[:300]}") from e


def _results(root):
    """/ResultSet/Result を [{タグ名: 値}] で返す。"""
    rows = []
    for result in root.iter():
        if _strip_ns(result.tag) != "Result":
            continue
        rows.append({_strip_ns(child.tag): (child.text or "").strip() for child in result})
    return rows


def recent_item_checks(results=5):
    """商品アップロードのデータチェック履歴（新しい順）。

    [{check_id, status, error_num, update_time}]
    """
    root = _get("/dataCheckHistorySummary",
                {"file_type": FILE_TYPE_ITEM, "results": int(results)})
    rows = []
    for row in _results(root):
        rows.append({"check_id": row.get("CheckId", ""),
                     "status": row.get("CheckStatus", ""),
                     "error_num": int(row.get("ErrorNum") or 0),
                     "update_time": row.get("UpdateTime", "")})
    return rows


def check_errors(check_id, results=100):
    """1回のアップロードの行ごとのエラー。

    [{line, code, message, data_key(商品コード), field, type}]
    """
    root = _get("/dataCheckHistoryDetail",
                {"check_id": check_id, "error_type": ERROR_TYPE_ERROR_ONLY,
                 "results": int(results)})
    rows = []
    for row in _results(root):
        rows.append({"line": row.get("ErrorLine", ""),
                     "code": row.get("ErrorCode", ""),
                     "message": row.get("ErrorMsg", ""),
                     "data_key": row.get("DataKey", ""),
                     "field": row.get("FieldName", ""),
                     "type": row.get("ErrorType", "")})
    return rows


def describe_latest_errors(codes=(), results=5):
    """直近の商品アップロードでYahooが出したエラーを、人が読める1行にまとめる。

    codes を渡すと、その商品コードに関係する行だけに絞る（空なら全件）。
    返り値: (説明のlist, 参照したcheck_id or "")
    """
    try:
        checks = recent_item_checks(results=results)
    except client.YahooError as e:
        return [f"データチェック履歴を取得できませんでした: {e}"], ""
    target = next((c for c in checks if c["error_num"] > 0), None)
    if not target:
        return [], (checks[0]["check_id"] if checks else "")
    try:
        errors = check_errors(target["check_id"])
    except client.YahooError as e:
        return [f"データチェック履歴の詳細を取得できませんでした: {e}"], target["check_id"]
    wanted = {str(c).strip().lower() for c in codes if str(c).strip()}
    lines = []
    for err in errors:
        key = str(err["data_key"]).strip().lower()
        if wanted and key and key not in wanted:
            continue
        who = err["data_key"] or f"行{err['line']}"
        field = err["field"] or "項目不明"
        lines.append(f"{who}（{field}）: {err['code']} {err['message']}")
    return lines, target["check_id"]
