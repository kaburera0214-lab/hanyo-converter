# -*- coding: utf-8 -*-
"""
ページ単位のロール認証ゲート（Secretsだけでオン/オフ・ユーザー・権限を管理）。

各ページの st.set_page_config() 直後に2行入れるだけで使う:
    from lib.auth import require_role
    require_role("payable")

Secrets 設定例（Streamlit Cloud の Secrets に貼る）:
    AUTH_ENABLED = "false"   # ← "true" にした瞬間に保護が有効。false/未設定なら従来どおり全開放

    [AUTH_USERS.taro]
    password = "taro-dake-no-pw"
    roles = ["all"]                      # 全ページOK（管理者）

    [AUTH_USERS.keiri-san]
    password = "keiri-dake-no-pw"
    roles = ["invoice", "payable"]       # 請求書発行と買掛のみ

    [AUTH_USERS.genba-san]
    password = "genba-dake-no-pw"
    roles = ["material"]                 # 資材・棚卸のみ

ロール: invoice=請求書発行(6-9) / payable=買掛・支払(10-13,18-19) / material=資材・棚卸(14-15) /
       event=イベントLP作成(16-17) / pricing=価格改定(20) / all=全部
- パスワードだけでログインする方式（誰のパスワードかで本人とロールを判定）なので、
  ユーザーごとに必ず別のパスワードにすること。
- ログインはブラウザセッション中1回だけ。ページ遷移のたびに聞かれることはない。
- AUTH_ENABLED が true なのに AUTH_USERS が空の場合は、安全側に倒して全ページアクセス不可になる。
"""
import streamlit as st

ROLE_LABELS = {
    "invoice": "請求書発行（売掛）",
    "payable": "買掛・支払",
    "material": "資材・棚卸",
    "event": "イベントLP作成",
    "pricing": "価格改定",
}

_USER_KEY = "auth_user"
_ROLES_KEY = "auth_roles"


def _enabled() -> bool:
    return str(st.secrets.get("AUTH_ENABLED", "false")).strip().lower() in ("true", "1", "yes", "on")


def _users() -> dict:
    try:
        return dict(st.secrets.get("AUTH_USERS", {}))
    except Exception:
        return {}


def require_role(role: str) -> None:
    """ページ冒頭で呼ぶ認証ゲート。

    - AUTH_ENABLED が false/未設定なら何もしない（従来どおり）。
    - 未ログインならログインフォームを出して st.stop()。
    - ログイン済みでもロールが無ければ拒否して st.stop()。
    """
    if not _enabled():
        return

    if st.session_state.get(_USER_KEY) is None:
        _login_form()
        st.stop()

    roles = st.session_state.get(_ROLES_KEY, [])
    if "all" in roles or role in roles:
        return

    st.error(
        f"このページ（{ROLE_LABELS.get(role, role)}）へのアクセス権がありません。"
        f"（ログイン中: {st.session_state.get(_USER_KEY)}）"
    )
    if st.button("別のユーザーでログインし直す"):
        st.session_state.pop(_USER_KEY, None)
        st.session_state.pop(_ROLES_KEY, None)
        st.rerun()
    st.stop()


def _login_form() -> None:
    st.markdown("### 🔐 ログイン")
    st.caption("このページの閲覧にはパスワードが必要です。")
    pw = st.text_input("パスワード", type="password", key="auth_pw_input")
    if st.button("ログイン", type="primary", key="auth_login_btn"):
        users = _users()
        if not users:
            st.error("ユーザーが設定されていません。管理者にSecrets（AUTH_USERS）の設定を依頼してください。")
            return
        for name, conf in users.items():
            if pw and pw == str(conf.get("password", "")):
                st.session_state[_USER_KEY] = name
                st.session_state[_ROLES_KEY] = [str(r) for r in conf.get("roles", [])]
                st.rerun()
        st.error("パスワードが正しくありません")
