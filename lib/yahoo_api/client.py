# -*- coding: utf-8 -*-
"""
Yahoo!ショッピング ストアAPIの認証（YConnect v2 認可コードフロー）。

- 認可: auth.login.yahoo.co.jp/yconnect/v2/authorization に店舗オーナーがログイン・同意
        → redirect_uri に code が返る
- 交換: POST /yconnect/v2/token（Basic認証=base64(client_id:client_secret)、
        grant_type=authorization_code）→ access_token / refresh_token
- 更新: grant_type=refresh_token。**店舗側でストアクリエイターProに公開鍵を登録済みなら
        リフレッシュトークンは28日有効**（未登録は12時間）。公開鍵は店舗単位なので既存ツールと共用でOK。

トークンはStreamlit Cloudのローカルが揮発するため Drive の yahoo_tokens.json に永続化する。
アクセストークンは短命(expires_in)なので、期限が近ければ自動リフレッシュする。

Secrets: YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_SELLER_ID / YAHOO_REDIRECT_URI
"""
import base64
import datetime
import json

import requests

AUTHORIZE_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/authorization"
TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
TOKENS_NAME = "yahoo_tokens.json"    # Drive（PRODUCT_MASTER_FOLDER_ID）に保存
TIMEOUT = (10, 20)                   # (接続, 読み取り)秒。長時間ハング防止
_SS_KEY = "_yahoo_tokens"
_LEEWAY = 120                        # アクセストークンの期限をこの秒数手前で更新する


class YahooError(RuntimeError):
    """Yahoo API呼び出しの失敗全般。"""


class YahooNotConfigured(YahooError):
    """SecretsにYahooのクライアントID/シークレット等が未設定。"""


class YahooAuthError(YahooError):
    """認証切れ・未認可（🔐から再認可が必要）。"""


def _secret(key, default=""):
    import streamlit as st
    return str(st.secrets.get(key, default)).strip()


def is_configured():
    return bool(_secret("YAHOO_CLIENT_ID")) and bool(_secret("YAHOO_CLIENT_SECRET"))


def seller_id():
    return _secret("YAHOO_SELLER_ID")


def redirect_uri():
    return _secret("YAHOO_REDIRECT_URI")


def _basic_header():
    cid = _secret("YAHOO_CLIENT_ID")
    secret = _secret("YAHOO_CLIENT_SECRET")
    if not cid or not secret:
        raise YahooNotConfigured("Secrets に YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET が未設定です。")
    token = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded"}


def authorize_url(state="recv", redirect=None):
    """店舗オーナーがログイン・同意する認可URL。承認後 redirect_uri に code が返る。"""
    import urllib.parse
    cid = _secret("YAHOO_CLIENT_ID")
    uri = redirect or redirect_uri()
    if not cid or not uri:
        raise YahooNotConfigured("Secrets に YAHOO_CLIENT_ID / YAHOO_REDIRECT_URI が必要です。")
    q = {"response_type": "code", "client_id": cid, "redirect_uri": uri,
         "scope": "openid", "state": state, "bail": "1"}
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)


def _folder_id():
    from lib import master_store
    return master_store.folder_id()


def _load_tokens():
    import streamlit as st
    cached = st.session_state.get(_SS_KEY)
    if cached:
        return cached
    from lib.invoice import drive_master
    f = drive_master.find_file(TOKENS_NAME, _folder_id())
    if not f:
        return None
    tokens = json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
    st.session_state[_SS_KEY] = tokens
    return tokens


def _save_tokens(access, refresh, expires_in):
    import streamlit as st
    from lib.invoice import drive_master
    now = datetime.datetime.now()
    tokens = {"access_token": access, "refresh_token": refresh,
              "expires_at": (now + datetime.timedelta(seconds=int(expires_in or 3600))).isoformat(),
              "saved_at": now.isoformat(timespec="seconds")}
    st.session_state[_SS_KEY] = tokens
    drive_master.upload_or_replace(
        json.dumps(tokens, ensure_ascii=False, indent=2).encode("utf-8"),
        TOKENS_NAME, _folder_id(), mimetype="application/json")
    return tokens


def token_status():
    """管理UI表示用（saved_at/expires_at）。未認可なら None。"""
    try:
        return _load_tokens()
    except Exception:  # noqa: BLE001
        return None


def exchange_code(code, redirect=None):
    """認可コードをトークンに交換してDriveへ保存する（初回認可・再認可）。"""
    uri = redirect or redirect_uri()
    res = requests.post(TOKEN_URL, headers=_basic_header(),
                        data={"grant_type": "authorization_code", "code": code,
                              "redirect_uri": uri}, timeout=TIMEOUT)
    data = _parse(res)
    if "access_token" not in data:
        raise YahooAuthError(f"Yahoo認証に失敗しました: {data}")
    return _save_tokens(data["access_token"], data.get("refresh_token"),
                        data.get("expires_in"))


def _refresh(refresh_token):
    res = requests.post(TOKEN_URL, headers=_basic_header(),
                        data={"grant_type": "refresh_token",
                              "refresh_token": refresh_token}, timeout=TIMEOUT)
    data = _parse(res)
    if "access_token" not in data:
        raise YahooAuthError("Yahooのアクセストークン更新に失敗しました（再認可が必要）: "
                             f"{data.get('error_description') or data}")
    # リフレッシュトークンは返らないこともある（その場合は既存を保持）
    return _save_tokens(data["access_token"],
                        data.get("refresh_token") or refresh_token,
                        data.get("expires_in"))


def _parse(res):
    try:
        return res.json()
    except ValueError:
        raise YahooError(f"Yahooトークン応答を解釈できません（HTTP {res.status_code}）: "
                         f"{res.text[:300]}")


def access_token():
    """有効なアクセストークンを返す。期限切れ間近なら自動更新する。"""
    tokens = _load_tokens()
    if not tokens:
        raise YahooAuthError("Yahoo APIが未認可です。🔐「Yahoo API接続」から認可してください。")
    try:
        exp = datetime.datetime.fromisoformat(tokens.get("expires_at", ""))
    except ValueError:
        exp = datetime.datetime.now()
    if datetime.datetime.now() + datetime.timedelta(seconds=_LEEWAY) >= exp:
        if not tokens.get("refresh_token"):
            raise YahooAuthError("Yahooのリフレッシュトークンがありません。再認可してください。")
        tokens = _refresh(tokens["refresh_token"])
    return tokens["access_token"]
