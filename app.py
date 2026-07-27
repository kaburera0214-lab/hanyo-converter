import streamlit as st

st.set_page_config(page_title="パピー業務ツール", layout="wide")

# ネクストエンジンAPIの認可コールバック受け口（redirect_uri=このアプリのルートURL）。
# NEで承認すると ?uid=..&state=.. が付いて戻ってくるので、その場でトークンに交換する。
_qp = st.query_params
if "uid" in _qp and "state" in _qp:          # ネクストエンジンの認可コールバック
    from lib.ne_api import client as ne_client
    try:
        ne_client.exchange(_qp["uid"], _qp["state"])
        st.success("✅ ネクストエンジンAPIの認可が完了しました（トークンをDriveに保存）。"
                   "左のメニューから「📥 入荷登録」に戻ってください。")
    except Exception as e:  # noqa: BLE001
        st.error(f"ネクストエンジンAPIの認可に失敗しました: {e}")
    st.query_params.clear()
elif "code" in _qp:                          # Yahoo（YConnect）の認可コールバック
    from lib.yahoo_api import client as yahoo_client
    try:
        yahoo_client.exchange_code(_qp["code"])
        st.success("✅ Yahoo APIの認可が完了しました（トークンをDriveに保存）。"
                   "「📥 入荷登録」に戻ってください。")
    except Exception as e:  # noqa: BLE001
        st.error(f"Yahoo APIの認可に失敗しました: {e}")
    st.query_params.clear()

st.title("パピー業務ツール")
st.markdown("左のメニューからページを選択してください。")

# NE APIの使用量（月間の呼び出し回数・通信量）。無料枠に近づくと警告。
# 全機能共通のため、個別ページに依存せずホームで常時表示する。
try:
    from lib.ne_api import client as _ne_client, usage as _ne_usage
    if _ne_client.is_configured():
        st.divider()
        st.markdown("#### 🔌 NE API 使用量（今月）")
        _ne_usage.render()
        st.caption("NEは月1000回まで無料（超過は課金）。マスタ自動取得は1回で約12回消費します。")
except Exception:  # noqa: BLE001
    pass
