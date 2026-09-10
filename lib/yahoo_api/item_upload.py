# -*- coding: utf-8 -*-
"""
Yahoo!ショッピング 商品アップロードAPI（uploadItemFile）。

  POST {BASE}/uploadItemFile?seller_id=...   （multipart/form-data）
    file … 商品データCSV
    type … 1:追加 / 2:上書き(全入れ替え) / 3:削除 / 4:項目指定

**このツールが使うのは type=4（項目指定）だけ。** ストアクリエイターProの
「商品データアップロード → アップロードタイプ＝項目指定」と同じもので、
CSVに書いた列だけを更新し、書かなかった列には触らない。

🚫 type=2（上書き）は**ストアの商品を全消去してCSVと入れ替える**。
🚫 商品登録API（editItem）は、1商品の中で**省略した項目をデフォルト値で上書きする**
   （商品名・価格・カテゴリ・説明文などが初期値に戻る）。
どちらもこのシステムでは使わない。詳細は AIworkspace/CLAUDE.md の禁止事項を参照。

なお uploadItemFile は受け付けた時点で応答を返し、**フロント反映はしない**。
反映は reservePublish（items.reserve_publish）で予約する。反映は非同期なので、
「送った」＝「反映された」とはみなさず、getItem で読み直して確認する
（確認は batch/yahoo_queue_watch.py が日次で行う）。
"""
import threading
import time
import xml.etree.ElementTree as ET

import requests

from . import client

PROD_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TEST_BASE = "https://test.circus.shopping.yahooapis.jp/ShoppingWebService/V1"
TIMEOUT = (10, 60)          # CSV送信なので読み取りは長めに
TYPE_FIELD_SPECIFIED = 4    # 項目指定（このツールが使う唯一のtype）
_MIN_INTERVAL = 1.1         # 公式の上限が「1クエリー/秒」なので、少し余裕を持って空ける
_last_call = [0.0]
_lock = threading.Lock()


def _rate_limit():
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _base():
    return TEST_BASE if client._secret("YAHOO_USE_TEST").lower() in ("true", "1", "yes") \
        else PROD_BASE


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _messages(text):
    """応答XMLから Status と エラーメッセージを取り出す。"""
    status, errors = "", []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return "", [f"応答XMLを解釈できません: {text[:300]}"]
    for el in root.iter():
        key = _strip_ns(el.tag)
        value = (el.text or "").strip()
        if not value:
            continue
        if key == "Status" and not status:
            status = value
        elif key in ("Message", "Detail", "ErrorMessage", "Description"):
            errors.append(value)
    return status, errors


def upload_field_specified(csv_bytes, filename="upload.csv"):
    """商品データCSVを**項目指定（type=4）**でアップロードする。

    返り値: (ok, errors)。ok=True は「Yahooがファイルを受け付けた」まで。
    行ごとの検証結果と店頭反映は非同期なので、**成功＝反映済みではない**。
    反映の確認は getItem で読み直して行うこと。
    """
    seller = client.seller_id()
    if not seller:
        raise client.YahooNotConfigured("Secrets に YAHOO_SELLER_ID が未設定です。")
    _rate_limit()
    url = f"{_base()}/uploadItemFile"
    res = requests.post(
        url,
        headers={"Authorization": f"Bearer {client.access_token()}"},
        params={"seller_id": seller},
        data={"type": str(TYPE_FIELD_SPECIFIED)},
        files={"file": (filename, csv_bytes, "text/csv")},
        timeout=TIMEOUT)
    if res.status_code in (401, 403):
        raise client.YahooAuthError(
            f"Yahoo APIの認証に失敗しました（HTTP {res.status_code}）。再認可してください。")
    if res.status_code >= 400:
        status, errors = _messages(client.decode(res))
        detail = "／".join(errors) if errors else client.decode(res)[:300]
        raise client.YahooError(f"商品アップロードに失敗しました（HTTP {res.status_code}）: {detail}")
    body = client.decode(res)
    status, errors = _messages(body)
    if status.upper() == "OK" and not errors:
        return True, []
    return False, errors or [f"Statusが OK ではありません: {status or '(空)'}／{body[:300]}"]
