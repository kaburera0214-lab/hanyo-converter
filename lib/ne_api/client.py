# -*- coding: utf-8 -*-
"""
ネクストエンジンAPI 共通クライアント（rms_api と同じ薄いクライアント様式）。

認証はNE独自のuid/state方式:
  1. auth_url() のURLをブラウザで開いてNEにログイン・承認
  2. redirect_uri に uid & state が付いて戻る（アプリ自身 or GitHub Pagesコールバック）
  3. exchange(uid, state) でアクセストークン・リフレッシュトークンを取得

トークンはStreamlit Cloudのローカルが揮発するため Drive の ne_tokens.json に永続化する。
毎API呼び出しで access+refresh を同送すると、access_tokenが期限切れでもrefresh_tokenが
生きていればレスポンスに新トークンが含まれる（自動リフレッシュ）ので、その都度Driveへ
差し替え保存する。

【NEのトークン期限（公式仕様）】
  access_token  … 発行から1日
  refresh_token … 発行から3日
  → 3日間1度もAPIを呼ばないと refresh_token ごと失効し、ブラウザでの再認可が必須になる
    （このときのエラーもaccess_token期限切れと同じ 002004）。NE公式も「バッチ等で定期的に
    利用する場合は2日より前にAPIを実行して期限を切らさない」運用を推奨している。
  → そのための延命バッチが batch/ne_keepalive.py（GitHub Actions で毎日実行）。
    keep_alive() を呼ぶだけでトークンが転がり続けるため、実質再認可不要になる。

【古いトークンを掴んだときの自動復帰】
  新トークンが発行されると旧トークンは即失効する。別プロセス（延命バッチ・週次マスタ同期）が
  更新した直後は、開きっぱなしのStreamlitセッションが持つキャッシュが古くなり002004になる。
  call() は認証系エラー時にDriveを読み直し、内容が違えば1度だけ再試行して自動復帰する。

切れたら NEAuthError → 画面の🔐expanderから再認可（NE仕様上ブラウザでのログインが必須）。

Secrets: NE_CLIENT_ID / NE_CLIENT_SECRET / NE_REDIRECT_URI
"""
import json
import time

import requests

API_BASE = "https://api.next-engine.org/"
SIGN_IN_URL = "https://base.next-engine.org/users/sign_in/"
TOKENS_NAME = "ne_tokens.json"   # Drive（PRODUCT_MASTER_FOLDER_ID）に保存
TIMEOUT = 60                     # 商品マスタuploadは数万行も想定して長め
_SS_KEY = "_ne_tokens"           # session_stateキャッシュ（Drive往復の削減）

REFRESH_TOKEN_HOURS = 72         # NE仕様: refresh_tokenは発行から3日
WARN_HOURS = 48                  # 残り1日を切ったら画面で警告（延命バッチの失敗に気づくため）
KEEPALIVE_ENDPOINT = "api_v1_login_company/info"   # 最も軽い疎通用エンドポイント（1回）


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


def _load_tokens(force_reload=False):
    """トークンを session → Drive の順で読む。無ければ None。
    force_reload=True でセッションキャッシュを捨ててDriveの最新を読む
    （別プロセスがトークンを更新した直後の復帰用）。"""
    import streamlit as st
    if not force_reload:
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
    """トークンを session と Drive の両方へ保存する。
    Driveへの保存に失敗すると旧トークンが失効済みのまま残り、次回以降ずっと認証切れに
    なってしまうため、数回リトライする（それでも駄目なら例外＝再認可を促す）。"""
    import datetime

    import streamlit as st
    from lib.invoice import drive_master
    tokens = dict(tokens)
    now = datetime.datetime.now()
    tokens["saved_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    tokens["saved_at_ts"] = int(time.time())   # 期限計算用（実行環境のTZに左右されない）
    st.session_state[_SS_KEY] = tokens
    payload = json.dumps(tokens, ensure_ascii=False, indent=2).encode("utf-8")
    last = None
    for attempt in range(3):
        try:
            drive_master.upload_or_replace(payload, TOKENS_NAME, _folder_id(),
                                           mimetype="application/json")
            return tokens
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise NEError(f"新しいNEトークンをDriveに保存できませんでした（{last}）。"
                  "🔐「NE API接続」から再認可してください。")


def token_status():
    """管理UI表示用: {saved_at} など。未認可なら None。"""
    try:
        return _load_tokens()
    except Exception:  # noqa: BLE001
        return None


def remaining_hours(saved_at_ts, now_ts=None):
    """refresh_tokenの推定残り時間（純関数・テスト対象）。saved_at_tsが無ければNone。"""
    if not saved_at_ts:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    return REFRESH_TOKEN_HOURS - (now_ts - float(saved_at_ts)) / 3600.0


def health_level(remaining, warn_hours=WARN_HOURS):
    """残り時間 → 表示レベル（純関数・テスト対象）。
    unknown（旧形式で残りが計算できない）/ ok / warn / dead。"""
    if remaining is None:
        return "unknown"
    if remaining <= 0:
        return "dead"
    if remaining <= (REFRESH_TOKEN_HOURS - warn_hours):
        return "warn"
    return "ok"


def health():
    """認可状態のサマリ（画面表示用）: {state, saved_at, remaining_hours}。
    state: none（未認可）/ unknown / ok / warn / dead。"""
    tokens = token_status()
    if not tokens:
        return {"state": "none", "saved_at": "", "remaining_hours": None}
    rest = remaining_hours(tokens.get("saved_at_ts"))
    return {"state": health_level(rest), "saved_at": tokens.get("saved_at", "不明"),
            "remaining_hours": rest}


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


def call(endpoint, params=None, _retried=False):
    """
    POST {API_BASE}{endpoint}。access_token/refresh_token をフォームで同送し、
    レスポンスに新トークンが含まれたら差し替え保存する（自動リフレッシュ）。
    result != success は例外化: 認証系（要再認可）→ NEAuthError / それ以外 → NEError

    認証系エラー時は、掴んでいたのが古いトークンだった可能性（別プロセスが更新した直後）を
    確かめるためDriveを読み直し、中身が変わっていれば1度だけ再試行する。
    """
    tokens = _load_tokens(force_reload=_retried)
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
        if not _retried and _tokens_changed_on_drive(tokens):
            return call(endpoint, params, _retried=True)   # 古いトークンを掴んでいただけ
        raise NEAuthError(f"NE APIの認証が切れています（{code} {message}）。"
                          "🔐「NE API接続」から再認可してください。")
    raise NEError(f"NE APIエラー（{endpoint}）: {code} {message}")


def _tokens_changed_on_drive(used):
    """Driveのトークンが、今回使ったものと違うか（=別プロセスが更新済みか）。
    無駄な再試行でAPI回数を消費しないよう、実際に変わっているときだけTrueを返す。"""
    try:
        fresh = _load_tokens(force_reload=True)
    except Exception:  # noqa: BLE001
        return False
    return bool(fresh) and fresh.get("access_token") != used.get("access_token")


def keep_alive():
    """トークン延命のための最小API呼び出し（batch/ne_keepalive.py から毎日実行する）。

    NEのrefresh_tokenは3日で切れるため、2日以内に1回APIを呼べばトークンが更新され続け、
    再認可なしで使い続けられる。返り値: {ok, endpoint, rotated, saved_at}。
    認証切れ（=既に3日以上放置された）は NEAuthError のまま投げる（人手の再認可が必要）。
    """
    before = (_load_tokens() or {}).get("access_token", "")
    result = call(KEEPALIVE_ENDPOINT)
    after = (_load_tokens() or {})
    return {"ok": True, "endpoint": KEEPALIVE_ENDPOINT,
            "rotated": bool(after.get("access_token") and after["access_token"] != before),
            "saved_at": after.get("saved_at", ""),
            "company": (result.get("data") or [{}])[0].get("company_name", "")
            if isinstance(result.get("data"), list) else ""}
