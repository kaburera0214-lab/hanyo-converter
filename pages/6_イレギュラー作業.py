# -*- coding: utf-8 -*-
"""
イレギュラー作業入力ページ（[汎用]作業料の元データ）

日々のイレギュラー作業（入庫/出庫/電話対応等）を、スプレッドシート感覚で
クライアント×対象月ごとに記録する。請求書発行ページがこの月次合計人時 ×
時給単価で [汎用]作業料 を自動算出する。

このページは完全独立。session_stateキーは "irr_"/"invoice_" 接頭辞で分離。
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="イレギュラー作業入力", layout="wide")
st.title("イレギュラー作業入力")
st.caption("TeamEC様などのイレギュラー作業を記録します。請求書の[汎用]作業料に自動反映されます。")

from lib.invoice import store, notion_store, csv_import


# --- Notion初期化（請求書ページと同じdb_idsを共有） ---
if not st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", ""):
    st.warning("Notion未設定のため保存できません。Secretsに INVOICE_NOTION_PARENT_PAGE_ID を設定してください。")
    st.stop()

try:
    cached = st.session_state.get("invoice_db_ids")
    if cached and all(k in cached for k in notion_store.DB_SCHEMAS):
        db_ids = cached
    else:
        with st.spinner("Notionデータベースを準備中…"):
            db_ids = notion_store.ensure_databases()
            notion_store.seed_clients_if_empty(db_ids, store.DEFAULT_CLIENTS)
        st.session_state["invoice_db_ids"] = db_ids
except Exception as e:
    st.error(f"Notion初期化に失敗しました: {e}")
    st.stop()


# --- クライアント・対象月 ---
try:
    clients = notion_store.load_clients(db_ids) or store.load_clients()
except Exception:
    clients = store.load_clients()
client_names = list(clients.keys())

c1, c2, c3 = st.columns([2, 1, 1])
client_name = c1.selectbox("クライアント", client_names, key="irr_client")
today = datetime.date.today()
year = c2.number_input("対象年", min_value=2020, max_value=2100,
                       value=today.year, step=1, key="irr_year")
month = c3.selectbox("対象月", list(range(1, 13)), index=today.month - 1, key="irr_month")
target_ym = f"{int(year)}-{int(month):02d}"

# 時給単価（クライアント別・単価マスタの 費目=その他/種別=[汎用]作業料）
hourly = 0.0
try:
    for r in notion_store.load_price_master(db_ids, client_name):
        if r["費目"] == "その他" and "[汎用]" in str(r["種別"]):
            hourly = float(r["単価"] or 0)
            break
except Exception:
    pass
st.caption(f"時給単価: {hourly:,.0f} 円/h（「請求書発行」→単価マスタ管理で変更できます）")


# --- 既存データ読込（編集の土台） ---
reload_key = f"irr_loaded_{client_name}_{target_ym}"
if st.button("🔄 この月のデータを再読込", key="irr_reload"):
    st.session_state.pop(reload_key, None)
    st.rerun()

if reload_key not in st.session_state:
    try:
        existing = notion_store.load_irregular_work(db_ids, client_name, target_ym)
    except Exception as e:
        existing = []
        st.error(f"読込に失敗: {e}")
    st.session_state[reload_key] = existing
existing = st.session_state[reload_key]

base_df = pd.DataFrame(
    [{"日付": r["日付"], "時間数": r["時間数"], "人数": r["人数"],
      "作業項目": r["作業項目"], "作業詳細": r["作業詳細"], "備考": r["備考"]}
     for r in existing],
    columns=["日付", "時間数", "人数", "作業項目", "作業詳細", "備考"])


# --- CSV取込（既存スプレッドシートからの移行用） ---
with st.expander("スプレッドシートCSVから取込（移行用）", expanded=False):
    st.caption("列：日付 / 時間数(h) / 人数 / 作業項目 / 作業詳細 / 備考。取り込むと下の表に追加されます。")
    up = st.file_uploader("イレギュラー作業CSV", type=["csv"], key="irr_csv")
    if up is not None:
        try:
            imported = csv_import.parse_irregular_csv(up.getvalue())
            imp_df = pd.DataFrame(imported, columns=base_df.columns)
            base_df = pd.concat([base_df, imp_df], ignore_index=True)
            st.success(f"{len(imp_df)} 行を取り込みました（下の表で確認し『保存』してください）。")
        except Exception as e:
            st.error(f"CSV取込に失敗: {e}")


# --- 入力テーブル ---
st.markdown(f"#### {client_name}／{target_ym} の作業記録")
edited = st.data_editor(
    base_df,
    num_rows="dynamic",
    use_container_width=True,
    key="irr_editor",
    column_config={
        "日付": st.column_config.TextColumn("日付", help="例: 2026/04/14"),
        "時間数": st.column_config.NumberColumn("時間数(h)", step=0.25, min_value=0),
        "人数": st.column_config.NumberColumn("人数", step=1, min_value=0),
        "作業項目": st.column_config.TextColumn("作業項目", help="入庫/出庫/電話 など"),
        "作業詳細": st.column_config.TextColumn("作業詳細", width="large"),
        "備考": st.column_config.TextColumn("備考"),
    },
)

# 合計人時・概算金額
edited2 = edited.copy()
edited2["合計時間"] = (pd.to_numeric(edited2["時間数"], errors="coerce").fillna(0)
                    * pd.to_numeric(edited2["人数"], errors="coerce").fillna(0))
total_hours = float(edited2["合計時間"].sum())
amount = round(total_hours * hourly)

m1, m2, m3 = st.columns(3)
m1.metric("合計人時", f"{total_hours:g} h")
m2.metric("時給単価", f"{hourly:,.0f} 円/h")
m3.metric("[汎用]作業料（概算）", f"{amount:,} 円")

if st.button("💾 保存", key="irr_save", type="primary"):
    try:
        n = notion_store.replace_irregular_work(
            db_ids, client_name, target_ym, edited.to_dict("records"))
        st.session_state.pop(reload_key, None)
        st.success(f"{client_name}／{target_ym} の作業記録を保存しました（{n}件）。"
                   "請求書発行ページの[汎用]作業料に反映されます。")
    except Exception as e:
        st.error(f"保存に失敗しました: {e}")
