import streamlit as st

st.set_page_config(page_title="パピー業務ツール", layout="wide")

# ネクストエンジンAPIの認可コールバック受け口（redirect_uri=このアプリのルートURL）。
# NEで承認すると ?uid=..&state=.. が付いて戻ってくるので、その場でトークンに交換する。
_qp = st.query_params
if "uid" in _qp and "state" in _qp:
    from lib.ne_api import client as ne_client
    try:
        ne_client.exchange(_qp["uid"], _qp["state"])
        st.success("✅ ネクストエンジンAPIの認可が完了しました（トークンをDriveに保存）。"
                   "左のメニューから「📥 入荷登録」に戻ってください。")
    except Exception as e:  # noqa: BLE001
        st.error(f"ネクストエンジンAPIの認可に失敗しました: {e}")
    st.query_params.clear()

st.title("パピー業務ツール")
st.markdown("左のメニューからページを選択してください。")
