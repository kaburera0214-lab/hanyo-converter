# -*- coding: utf-8 -*-
"""
Yahoo!ショッピング ストアAPIの認証（YConnect v2 認可コードフロー）。

- 認可: auth.login.yahoo.co.jp/yconnect/v2/authorization に店舗オーナーがログイン・同意
        → redirect_uri に code が返る
- 交換: POST /yconnect/v2/token（Basic認証=base64(client_id:client_secret)、
        grant_type=authorization_code）→ access_token / refresh_token
- 更新: grant_type=refresh_token。**店舗側でストアクリエイターProに公開鍵を登録済みなら
        リフレッシュトークンは28日有効**（未登録は12時間）。公開鍵は店舗単位なので既存ツールと共用でOK。

【リフレッシュトークンは延命できない】
28日は**認可した日から固定**で、アクセストークンを更新しても延びない
（更新の応答に refresh_token が含まれない＝同じトークンを使い続ける）。
つまり28日ごとに、店舗オーナーがブラウザでログインして再認可するしかない。
だから認可した日時（authorized_at）を控え、期限を自分で数えて、切れる前に知らせる
（lib/auth_keepalive.py）。
  根拠: https://developer.yahoo.co.jp/yconnect/v2/authorization_code/token.html
        「Refresh Token … 有効期限は4週間です」／更新の応答は access_token・token_type・expires_in のみ
  実測: 2026-10-07 の強制更新で rotated=false。その前は最終更新（09-21）から16日で失効した
  最後に確認した日: 2026-10-07

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
REFRESH_LIFETIME_DAYS = 28           # リフレッシュトークンの寿命（認可した日から固定。冒頭の根拠を参照）
# authorized_at を控え始める前（〜2026-10-07）に保存されたトークン用の認可日時。
# 2026-10-07 の再認可で画面に出た「トークン保存: 2026-10-07T05:43:05」が出どころ。
# 次に再認可すれば authorized_at がファイルに入るので、この値は使われなくなる。
_LEGACY_AUTHORIZED_AT = "2026-10-07T05:43:05"


def decode(res):
    """Yahooの応答を文字化けさせずに読む。

    Content-Type に charset が無いと requests は ISO-8859-1 とみなすため、
    日本語のエラーメッセージがキリル文字などに化けて原因が読めなくなる
    （2026-09-10 に reservePublish のエラーで実際に踏んだ）。
    宣言どおり UTF-8 を優先し、だめなら日本語の在来エンコーディングを試す。
    """
    raw = res.content or b""
    for encoding in ("utf-8", "euc_jp", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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


def api_enabled():
    """更新時にYahoo APIを使うか。YAHOO_DISABLE=true で一時的に無効化できる（切り分け用）。
    無効時はYahoo価格を更新できないため、価格更新を伴う画面では実行をブロックする。"""
    if str(_secret("YAHOO_DISABLE")).lower() in ("true", "1", "yes"):
        return False
    return is_configured()


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


def _load_tokens(force=False):
    """force=True は画面が覚えている分を捨てて Drive から読み直す。"""
    import streamlit as st
    cached = None if force else st.session_state.get(_SS_KEY)
    if cached:
        return cached
    from lib.invoice import drive_master
    f = drive_master.find_file(TOKENS_NAME, _folder_id())
    if not f:
        return None
    tokens = json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
    st.session_state[_SS_KEY] = tokens
    return tokens


def _save_tokens(access, refresh, expires_in, authorized_at=""):
    """authorized_at: 店舗オーナーが認可した日時。28日の期限はここから数える。"""
    import streamlit as st
    from lib.invoice import drive_master
    now = datetime.datetime.now()
    tokens = {"access_token": access, "refresh_token": refresh,
              "expires_at": (now + datetime.timedelta(seconds=int(expires_in or 3600))).isoformat(),
              "saved_at": now.isoformat(timespec="seconds"),
              "authorized_at": authorized_at or now.isoformat(timespec="seconds")}
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


def authorized_at(tokens):
    """認可した日時。読めなければ None（＝期限を判定できない。0日や無期限に丸めない）。"""
    raw = str((tokens or {}).get("authorized_at") or _LEGACY_AUTHORIZED_AT).strip()
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def reauth_deadline(tokens):
    """再認可の期限（＝リフレッシュトークンが切れる日時）。判定できなければ None。"""
    at = authorized_at(tokens)
    return at + datetime.timedelta(days=REFRESH_LIFETIME_DAYS) if at else None


def days_until_reauth(tokens, now=None):
    """再認可の期限まであと何日か（切り捨て。切れていれば負）。判定できなければ None。"""
    deadline = reauth_deadline(tokens)
    if deadline is None:
        return None
    seconds = (deadline - (now or datetime.datetime.now())).total_seconds()
    return int(seconds // 86400)


def deadline_text(tokens, now=None):
    """画面・通知に出す期限の文言（日本時間）。サーバーの時計はUTCなので9時間足す。"""
    deadline = reauth_deadline(tokens)
    days = days_until_reauth(tokens, now)
    if deadline is None or days is None:
        return "再認可の期限を判定できません（認可した日時が読めません）"
    jst = deadline + datetime.timedelta(hours=9)
    if days < 0:
        return f"再認可の期限（{jst:%Y-%m-%d}）を過ぎています"
    return f"次の再認可の期限: {jst:%Y-%m-%d}（あと{days}日）"


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


def _refresh(refresh_token, authorized=""):
    res = requests.post(TOKEN_URL, headers=_basic_header(),
                        data={"grant_type": "refresh_token",
                              "refresh_token": refresh_token}, timeout=TIMEOUT)
    data = _parse(res)
    if "access_token" not in data:
        raise YahooAuthError("Yahooのアクセストークン更新に失敗しました（再認可が必要）: "
                             f"{data.get('error_description') or data}")
    # 更新の応答に refresh_token は含まれない（既存を保持）。期限も延びないので、
    # 認可した日時は引き継ぐ（ここで今の時刻にすると、期限を28日先と誤って数える）。
    return _save_tokens(data["access_token"],
                        data.get("refresh_token") or refresh_token,
                        data.get("expires_in"), authorized_at=authorized)


def _authorized_raw(tokens):
    at = authorized_at(tokens)
    return at.isoformat(timespec="seconds") if at else ""


def _refresh_tokens(tokens):
    """アクセストークンを更新する。失敗したら Drive を読み直して1回だけやり直す。

    画面（セッション）は読んだトークンを覚えている。別の画面で再認可されても
    古いほうを使い続けるので、「再認可したのに同じ認証切れが出る」ことになる
    （2026-10-07 実例: 管理者が再認可しても、スタッフの画面の再実行は通らない状態だった）。
    """
    try:
        return _refresh(tokens["refresh_token"], _authorized_raw(tokens))
    except YahooAuthError:
        fresh = _load_tokens(force=True)
        if not fresh or not fresh.get("refresh_token") \
                or fresh.get("refresh_token") == tokens.get("refresh_token"):
            raise                       # Drive側も同じ＝本当に切れている
        return _refresh(fresh["refresh_token"], _authorized_raw(fresh))


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
        tokens = _refresh_tokens(tokens)
    return tokens["access_token"]


def keep_alive():
    """アクセストークンを強制更新し、再認可の期限まであと何日かを返す
    （batch/auth_keepalive.py から毎日実行する）。

    **これでリフレッシュトークンの28日は延びない**（冒頭の説明を参照）。
    ここでやっているのは「いま使えるか」の確認と、期限の数え直しだけ。
    2026-10-07 までは「更新のたびに28日が巻き直る」前提で書かれており、
    実際には延びていなかった。

    返り値: {ok, rotated, saved_at, expires_at, days_left, deadline_text}。
    days_left が None のときは期限を判定できていない（呼び出し側で異常として扱う）。
    認証切れは YahooAuthError のまま投げる＝ブラウザでの再認可が必要で、自動化できない。
    """
    tokens = _load_tokens(force=True)   # 画面で再認可された直後でも最新を使う
    if not tokens:
        raise YahooAuthError("Yahoo APIが未認可です。🔐「Yahoo API接続」から認可してください。")
    before = tokens.get("refresh_token", "")
    if not before:
        raise YahooAuthError("Yahooのリフレッシュトークンがありません。再認可してください。")
    after = _refresh(before, _authorized_raw(tokens))
    return {"ok": True,
            "rotated": bool(after.get("refresh_token") and after["refresh_token"] != before),
            "saved_at": after.get("saved_at", ""),
            "expires_at": after.get("expires_at", ""),
            "days_left": days_until_reauth(after),
            "deadline_text": deadline_text(after)}
