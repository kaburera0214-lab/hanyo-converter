# -*- coding: utf-8 -*-
"""
ネクストエンジンAPI 共通クライアント（rms_api と同じ薄いクライアント様式）。

認証はNE独自のuid/state方式:
  1. auth_url() のURLをブラウザで開いてNEにログイン・承認
  2. redirect_uri に uid & state が付いて戻る（アプリ自身 or GitHub Pagesコールバック）
  3. exchange(uid, state) でアクセストークン・リフレッシュトークンを取得

トークンはStreamlit Cloudのローカルが揮発するため Drive の ne_tokens.json に永続化する。
毎API呼び出しで access+refresh を同送すると、期限が近い場合レスポンスに新トークンが
含まれる（自動リフレッシュ）ので、その都度Driveへ差し替え保存する。
＝定常利用していればほぼ切れない。切れたら NEAuthError → 画面の🔐expanderから再認可。

Secrets: NE_CLIENT_ID / NE_CLIENT_SECRET / NE_REDIRECT_URI
"""
import json

import requests

API_BASE = "https://api.next-engine.org/"
SIGN_IN_URL = "https://base.next-engine.org/users/sign_in/"
TOKENS_NAME = "ne_tokens.json"   # Drive（PRODUCT_MASTER_FOLDER_ID）に保存
TIMEOUT = 60                     # 商品マスタuploadは数万行も想定して長め
_SS_KEY = "_ne_tokens"           # session_stateキャッシュ（Drive往復の削減）


class NEError(RuntimeError):
    """NE API呼び出しの失敗全般。"""


class NENotConfigured(NEError):
    """SecretsにNEのクライアントID/シークレットが未設定。"""


class NEAuthError(NEError):
    """認証切れ・未認可（🔐expanderからの再認可が必要）。"""


def _secrets():
    import streamlit as st
    cid = str(st.secrets.get("NE_CLIENT_ID", "")).strip()
    secret = str(st.secrets.get("NE_CLIENT_SECRET", "")).strip()
    if not cid or not secret:
        raise NENotConfigured("Secrets に NE_CLIENT_ID / NE_CLIENT_SECRET が未設定です。")
    return cid, secret


def is_configured():
    try:
        _secrets()
        return True
    except NENotConfigured:
        return False


def redirect_uri():
    import streamlit as st
    return str(st.secrets.get("NE_REDIRECT_URI", "")).strip()


def auth_url(redirect=None):
    """NEのログイン画面URL（正式仕様: /users/sign_in/?client_id=…）。
    ログイン完了後、アプリに登録済みのRedirect URIへ uid&state が付いて戻る。
    redirect_uriパラメータは登録値と一致する場合のみ付与（不一致だと弾かれるため）。"""
    import urllib.parse
    cid, _secret = _secrets()
    url = f"{SIGN_IN_URL}?client_id={cid}"
    uri = redirect or redirect_uri()
    if uri:
        url += "&redirect_uri=" + urllib.parse.quote(uri, safe="")
    return url


def _folder_id():
    from lib import master_store
    return master_store.folder_id()


def _load_tokens():
    """トークンを session → Drive の順で読む。無ければ None。"""
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


def _save_tokens(tokens):
    """トークンを session と Drive の両方へ保存する。"""
    import datetime

    import streamlit as st
    from lib.invoice import drive_master
    tokens = dict(tokens)
    tokens["saved_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state[_SS_KEY] = tokens
    drive_master.upload_or_replace(
        json.dumps(tokens, ensure_ascii=False, indent=2).encode("utf-8"),
        TOKENS_NAME, _folder_id(), mimetype="application/json")
    return tokens


def token_status():
    """管理UI表示用: {saved_at} など。未認可なら None。"""
    try:
        return _load_tokens()
    except Exception:  # noqa: BLE001
        return None


def exchange(uid, state):
    """uid/state をトークンに交換してDriveへ保存する（初回認可・再認可）。"""
    cid, secret = _secrets()
    res = requests.post(API_BASE + "api_neauth",
                        data={"uid": uid, "state": state,
                              "client_id": cid, "client_secret": secret},
                        timeout=TIMEOUT)
    try:
        data = res.json()
    except ValueError:
        raise NEError(f"NE認証レスポンスを解釈できません: {res.text[:300]}")
    if "access_token" not in data:
        raise NEAuthError(f"NE認証に失敗しました: {data}")
    return _save_tokens({"access_token": data["access_token"],
                         "refresh_token": data["refresh_token"]})


def call(endpoint, params=None):
    """
    POST {API_BASE}{endpoint}。access_token/refresh_token をフォームで同送し、
    レスポンスに新トークンが含まれたら差し替え保存する（自動リフレッシュ）。
    result != success は例外化: 認証系（要再認可）→ NEAuthError / それ以外 → NEError
    """
    tokens = _load_tokens()
    if not tokens:
        raise NEAuthError("ネクストエンジンAPIが未認可です。🔐「NE API接続」から認可してください。")
    data = {"access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"]}
    if params:
        data.update(params)
    res = requests.post(API_BASE + endpoint, data=data, timeout=TIMEOUT)
    try:                                  # 全NE API呼び出しを横断でカウント（課金監視）
        from . import usage
        usage.record(1, len(res.content or b""))
    except Exception:  # noqa: BLE001
        pass
    try:
        result = res.json()
    except ValueError:
        raise NEError(f"NE APIレスポンスを解釈できません（HTTP {res.status_code}）: {res.text[:300]}")

    if "access_token" in result:  # トークンが更新された→即保存（旧トークンは失効する）
        _save_tokens({"access_token": result["access_token"],
                      "refresh_token": result["refresh_token"]})

    status = str(result.get("result", ""))
    if status == "success":
        return result
    code = str(result.get("code", ""))
    message = str(result.get("message", ""))
    # redirect=再ログインが必要 / 002***=認証系エラー（トークン不正・失効など）
    if status == "redirect" or code.startswith("002"):
        raise NEAuthError(f"NE APIの認証が切れています（{code} {message}）。"
                          "🔐「NE API接続」から再認可してください。")
    raise NEError(f"NE APIエラー（{endpoint}）: {code} {message}")
