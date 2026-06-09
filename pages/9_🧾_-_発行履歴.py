# -*- coding: utf-8 -*-
"""
発行履歴ビューア（過去の請求書・見積の一覧／再ダウンロード）

請求_発行履歴 のスナップショットを一覧表示し、選んだ請求書のMF CSVを
当時の内容のまま再生成してダウンロードできる。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="発行履歴", layout="wide")
st.title("発行履歴（請求書・見積）")
st.caption("過去に確定した請求書を一覧・再ダウンロードできます。")

from lib.invoice import store, notion_store, mf_export

if not st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", ""):
    st.warning("Notion未設定のため履歴を表示できません。Secretsに INVOICE_NOTION_PARENT_PAGE_ID を設定してください。")
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

try:
    clients = notion_store.load_clients(db_ids) or store.load_clients()
except Exception:
    clients = store.load_clients()
client_names = list(clients.keys())

f1, f2 = st.columns([2, 2])
client_filter = f1.selectbox("クライアントで絞り込み", ["（すべて）"] + client_names,
                             key="hist_client")
kw = f2.text_input("対象年月で絞り込み（例 2026-05・空欄で全件）", key="hist_ym")

if st.button("🔄 最新に更新", key="hist_reload"):
    st.session_state.pop("hist_rows", None)
    st.rerun()

if "hist_rows" not in st.session_state:
    try:
        st.session_state["hist_rows"] = notion_store.load_issue_history(db_ids)
    except Exception as e:
        st.session_state["hist_rows"] = []
        st.error(f"履歴の読込に失敗: {e}")
rows = st.session_state["hist_rows"]

# フィルタ
view = rows
if client_filter != "（すべて）":
    view = [r for r in view if r["クライアント"] == client_filter]
if kw.strip():
    view = [r for r in view if kw.strip() in r["対象年月"]]

if not view:
    st.info("該当する発行履歴がありません。")
    st.stop()

st.markdown(f"#### 一覧（{len(view)}件）")
st.dataframe(
    pd.DataFrame([
        {"請求書番号": r["請求書番号"], "区分": r["区分"], "クライアント": r["クライアント"],
         "対象年月": r["対象年月"], "請求日": r["請求日"],
         "合計金額": f"{int(r['合計金額']):,}", "発行日時": r["発行日時"]} for r in view]),
    use_container_width=True, hide_index=True)

# 個別表示・再DL
labels = [f"{r['請求書番号']}（{r['区分']}・{r['対象年月']}・{int(r['合計金額']):,}円・{r['発行日時']}）"
          for r in view]
idx = st.selectbox("詳細を見る／再ダウンロードする請求書を選択",
                   range(len(view)), format_func=lambda i: labels[i], key="hist_sel")
rec = view[idx]

st.markdown(f"#### {rec['請求書番号']}（{rec['区分']}）の内容")
items = rec["品目"]
if items:
    st.dataframe(
        pd.DataFrame([
            {"品名": it.get("品名"), "単価": f"{int(it.get('単価', 0)):,}",
             "数量": it.get("数量"), "金額": f"{int(it.get('金額', 0)):,}"} for it in items]),
        use_container_width=True, hide_index=True)
c1, c2, c3 = st.columns(3)
c1.metric("小計", f"{int(rec['小計']):,} 円")
c2.metric("消費税", f"{int(rec['消費税']):,} 円")
c3.metric("合計金額", f"{int(rec['合計金額']):,} 円")

# MF CSV を当時の内容で再生成
cl = clients.get(rec["クライアント"], {})
h = cl.get("header", {})
header = {
    "取引先名称": h.get("取引先名称", ""), "件名": h.get("件名", ""),
    "請求日": rec["請求日"], "お支払期限": rec["支払期限"],
    "請求書番号": rec["請求書番号"], "売上計上日": rec["請求日"],
    "取引先郵便番号": h.get("取引先郵便番号", ""), "取引先都道府県": h.get("取引先都道府県", ""),
    "取引先住所1": h.get("取引先住所1", ""), "取引先住所2": h.get("取引先住所2", ""),
    "備考": h.get("備考", ""), "振込先": h.get("振込先", ""),
}
enc = st.radio("文字コード", ["UTF-8(BOM付き)", "Shift-JIS(cp932)"], horizontal=True,
               key="hist_enc")
encoding = "cp932" if enc.startswith("Shift") else "utf-8-sig"
try:
    csv_bytes = mf_export.to_csv_bytes(header, items, encoding=encoding)
    st.download_button("⬇️ MF請求書CSVを再ダウンロード", data=csv_bytes,
                       file_name=f"MF請求書_{rec['クライアント']}_{rec['請求書番号']}.csv",
                       mime="text/csv", key="hist_dl", type="primary")
    st.caption("※ 取引先住所・振込先は現在のマスタ値で再構成します（金額・品目は当時のまま）。")
except Exception as e:
    st.error(f"CSV再生成に失敗: {e}")
