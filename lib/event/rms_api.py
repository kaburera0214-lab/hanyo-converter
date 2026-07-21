# -*- coding: utf-8 -*-
"""
RMS WEB SERVICE 共通クライアント。

認証は全API共通で Authorization: ESA Base64(serviceSecret:licenseKey) ヘッダ。
Secrets: RMS_SERVICE_SECRET / RMS_LICENSE_KEY
ライセンスキーには有効期限があるため、401/403は「キー更新が必要」と分かる
例外(RMSAuthError)に変換して呼び出し側でバナー表示できるようにする。
"""
import base64

import requests

BASE_URL = "https://api.rms.rakuten.co.jp"
TIMEOUT = 20


class RMSError(RuntimeError):
    """RMS API呼び出しの失敗全般。"""


class RMSNotConfigured(RMSError):
    """Secretsにキーが未設定。"""


class RMSAuthError(RMSError):
    """認証失敗(ライセンスキー期限切れ/無効の可能性)。"""


def _get_secret(name):
    """Streamlit Secrets → 環境変数の順で探す。
    GitHub Actions等のstreamlitが無い環境でも動かすためのフォールバック。"""
    try:
        import streamlit as st
        v = str(st.secrets.get(name, "")).strip()
        if v:
            return v
    except Exception:
        pass
    import os
    return str(os.environ.get(name, "")).strip()


def is_configured():
    return bool(_get_secret("RMS_SERVICE_SECRET")) and \
        bool(_get_secret("RMS_LICENSE_KEY"))


def _headers():
    secret = _get_secret("RMS_SERVICE_SECRET")
    license_key = _get_secret("RMS_LICENSE_KEY")
    if not secret or not license_key:
        raise RMSNotConfigured(
            "Secrets に RMS_SERVICE_SECRET / RMS_LICENSE_KEY が未設定です。")
    token = base64.b64encode(f"{secret}:{license_key}".encode()).decode()
    return {"Authorization": f"ESA {token}"}


def _check(resp):
    if resp.status_code in (401, 403):
        raise RMSAuthError(
            f"RMS APIの認証に失敗しました(HTTP {resp.status_code})。"
            "ライセンスキーの有効期限切れの可能性があります。"
            "RMS「WEB APIサービス」でキーを更新し、Secretsを差し替えてください。")
    if resp.status_code >= 400:
        raise RMSError(f"RMS APIエラー HTTP {resp.status_code}: {resp.text[:500]}")
    return resp


def get(path, params=None):
    """GET {BASE_URL}{path} → JSON dict。"""
    resp = requests.get(BASE_URL + path, params=params,
                        headers=_headers(), timeout=TIMEOUT)
    return _check(resp).json()


def patch(path, json_body):
    """PATCH {BASE_URL}{path} (JSON) → status_code(204等はボディなし)。"""
    headers = _headers()
    headers["Content-Type"] = "application/json"
    resp = requests.patch(BASE_URL + path, json=json_body,
                          headers=headers, timeout=TIMEOUT)
    _check(resp)
    return resp.status_code


def post_xml(path, xml_body):
    """POST {BASE_URL}{path} (XML) → レスポンステキスト。Phase 2のクーポンAPI用。
    Content-Typeはapplication/xml必須(text/xmlだと『Request data is wrong format』で拒否される)。"""
    headers = _headers()
    headers["Content-Type"] = "application/xml; charset=utf-8"
    resp = requests.post(BASE_URL + path, data=xml_body.encode("utf-8"),
                         headers=headers, timeout=TIMEOUT)
    return _check(resp).text
