# -*- coding: utf-8 -*-
"""
RMS WEB SERVICE 共通クライアント。

認証は全API共通で Authorization: ESA Base64(serviceSecret:licenseKey) ヘッダ。
Secrets: RMS_SERVICE_SECRET / RMS_LICENSE_KEY
ライセンスキーには有効期限があるため、401/403は「キー更新が必要」と分かる
例外(RMSAuthError)に変換して呼び出し側でバナー表示できるようにする。

■ ライセンスキーは「共通の置き場」から読む（2026-11-04〜）
キーは約3か月で切れ、同じキーを管理者のPC・この Streamlit・GitHub Actions の3か所に
貼り直していた。いまは管理者のPCが共通の置き場（Supabase の関数 shared_secret_get）に
暗号文を置き、ここは使うたびに最新を読む（10分は使い回す）。人が貼るのは管理者のPCの
業務デスク1か所だけ。
- 読むのに要る Secrets（初回だけ）: SHARED_SECRETS_URL / SHARED_SECRETS_ANON_KEY / SHARED_SECRETS_KEY
- 置き場から読めないとき（未設定・通信・復号の失敗）は、これまでどおり RMS_LICENSE_KEY を使う。
  どちらを使っているかは key_source() で画面に出せる（黙って古いキーで動き続けない）
- このリポジトリは公開。キーの値・末尾4桁はログに出さない（GitHub Actions のログも公開）
"""
import base64
import os
import time

import requests

BASE_URL = "https://api.rms.rakuten.co.jp"
TIMEOUT = 20

SHARED_NAME = "rms_license_key"
SHARED_TTL = 600         # 置き場から読んだキーを使い回す秒数
SHARED_FAIL_TTL = 60     # 読めなかったことを覚えておく秒数（画面のたびに待たせない）
SHARED_TIMEOUT = 10
_shared = {"at": 0.0, "value": None, "ok": None}
_source = {"source": None, "detail": ""}


class RMSError(RuntimeError):
    """RMS API呼び出しの失敗全般。"""


class RMSNotConfigured(RMSError):
    """Secretsにキーが未設定。"""


class RMSAuthError(RMSError):
    """認証失敗(ライセンスキー期限切れ/無効の可能性)。"""


def _plain_secret(name):
    """Streamlit Secrets → 環境変数の順で探す。
    GitHub Actions等のstreamlitが無い環境でも動かすためのフォールバック。"""
    try:
        import streamlit as st
        v = str(st.secrets.get(name, "")).strip()
        if v:
            return v
    except Exception:
        pass
    return str(os.environ.get(name, "")).strip()


def _reader():
    return "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "streamlit"


def _set_source(source, detail):
    changed = _source.get("source") != source
    _source.update(source=source, detail=detail)
    if changed and _reader() == "github-actions":
        # 公開ログなので、どちらから読んだかだけを出す（値・末尾4桁は出さない）
        print(f"[rms_api] ライセンスキー: {detail}")


def _shared_license_key():
    """共通の置き場から最新のライセンスキーを読む。読めなければ None。"""
    url = _plain_secret("SHARED_SECRETS_URL")
    anon = _plain_secret("SHARED_SECRETS_ANON_KEY")
    fkey = _plain_secret("SHARED_SECRETS_KEY")
    if not (url and anon and fkey):
        _set_source("secrets", "共通の置き場が未設定のため、Secrets の RMS_LICENSE_KEY を使っています")
        return None
    now = time.time()
    ttl = SHARED_TTL if _shared["ok"] else SHARED_FAIL_TTL
    if _shared["ok"] is not None and now - _shared["at"] < ttl:
        return _shared["value"]
    try:
        resp = requests.post(
            url.rstrip("/") + "/rest/v1/rpc/shared_secret_get",
            json={"p_name": SHARED_NAME, "p_reader": _reader()},
            headers={"apikey": anon, "Authorization": f"Bearer {anon}"},
            timeout=SHARED_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise LookupError("置き場にキーがありません")
        from cryptography.fernet import Fernet
        value = Fernet(fkey.encode()).decrypt(rows[0]["ciphertext"].encode()).decode().strip()
        if not value:
            raise ValueError("空のキー")
    except Exception as exc:  # noqa: BLE001  通信・復号・未登録のどれでも、Secrets に戻る
        _shared.update(at=now, value=None, ok=False)
        _set_source("secrets", "共通の置き場から読めなかったため、Secrets の RMS_LICENSE_KEY を使っています"
                               f"（{type(exc).__name__}）")
        return None
    _shared.update(at=now, value=value, ok=True)
    exp = rows[0].get("expiry") or "不明"
    _set_source("shared", f"共通の置き場から読んでいます（期限 {exp}）")
    return value


def _get_secret(name):
    """ライセンスキーは共通の置き場を先に見る。ほかは Secrets → 環境変数。"""
    if name == "RMS_LICENSE_KEY":
        v = _shared_license_key()
        if v:
            return v
    return _plain_secret(name)


def key_source():
    """ライセンスキーをどこから読んでいるか（画面に出す用）。値は入っていない。

    {"source": "shared" | "secrets" | None, "detail": "..."}
    """
    if _source["source"] is None:
        _get_secret("RMS_LICENSE_KEY")
    return dict(_source)


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
            "RMS「WEB APIサービス」でキーを表示し、管理者のPCの業務デスク（RMSキー）に貼ってください"
            "（ここは共通の置き場から最新を読みます）。")
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
