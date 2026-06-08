# -*- coding: utf-8 -*-
"""
請求書発行ページ（Phase1）

このページは完全独立。既存ページ・共通ファイルには一切依存しない。
- session_state のキーはすべて "invoice_" 接頭辞で名前空間を分離
- import は遅延（関数内）にして、万一の不具合でもアプリ全体を巻き込まない

Phase1スコープ:
  1. クライアント選択（請求先ヘッダ情報を初期表示）
  2. 保管料入力（2期制：15日・末日の数量を入れて平均を自動計算）
  3. イレギュラー手入力（送料・作業料・値引き等を自由に追加）
  4. 請求書番号・各日付の自動生成（上書き可）
  5. MFクラウド取込用CSVのダウンロード（＋任意でDriveバックアップ）
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="請求書発行", layout="wide")
st.title("請求書発行")
st.caption("倉庫業務クライアント向けの請求書を作成し、MFクラウド取込用CSVを出力します。（Phase1）")

# --- 専用モジュール（遅延import） ---
from lib.invoice import mf_export, invoice_number, store


# ============================================================
# 1. クライアント選択
# ============================================================
clients = store.load_clients()
client_names = list(clients.keys())

st.header("① クライアント・対象月")
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    client_name = st.selectbox("クライアント", client_names, key="invoice_client")
with col2:
    today = datetime.date.today()
    # 既定は先月（締めて請求するため）
    default_year = today.year if today.month > 1 else today.year - 1
    year = st.number_input("対象年", min_value=2020, max_value=2100,
                           value=today.year, step=1, key="invoice_year")
with col3:
    default_month = today.month - 1 if today.month > 1 else 12
    month = st.selectbox("対象月", list(range(1, 13)),
                         index=default_month - 1, key="invoice_month")

client = clients[client_name]
client_code = client.get("略号", "XX")

# --- 採番・日付の初期値 ---
auto_no = invoice_number.generate_invoice_number(int(year), int(month), client_code)
auto_dates = invoice_number.default_dates(int(year), int(month))

st.header("② 請求書ヘッダ情報")
hcol1, hcol2, hcol3 = st.columns(3)
with hcol1:
    inv_no = st.text_input("請求書番号", value=auto_no, key="invoice_no")
    issue_date = st.text_input("請求日", value=auto_dates["請求日"], key="invoice_issue")
with hcol2:
    due_date = st.text_input("お支払期限", value=auto_dates["お支払期限"], key="invoice_due")
    sales_date = st.text_input("売上計上日", value=auto_dates["売上計上日"], key="invoice_sales")
with hcol3:
    subject = st.text_input("件名", value=client["header"].get("件名", ""), key="invoice_subject")
    staff = st.text_input("自社担当者氏名", value=client["header"].get("自社担当者氏名", ""),
                          key="invoice_staff")

with st.expander("取引先の詳細情報（住所・備考・振込先）", expanded=False):
    h = client["header"]
    ec1, ec2 = st.columns(2)
    with ec1:
        corp_name = st.text_input("取引先名称", value=h.get("取引先名称", ""), key="invoice_corp")
        zip_code = st.text_input("取引先郵便番号", value=h.get("取引先郵便番号", ""), key="invoice_zip")
        pref = st.text_input("取引先都道府県", value=h.get("取引先都道府県", ""), key="invoice_pref")
        addr1 = st.text_input("取引先住所1", value=h.get("取引先住所1", ""), key="invoice_addr1")
        addr2 = st.text_input("取引先住所2", value=h.get("取引先住所2", ""), key="invoice_addr2")
    with ec2:
        keisho = st.text_input("取引先敬称", value=h.get("取引先敬称", ""), key="invoice_keisho")
        biko = st.text_area("備考", value=h.get("備考", ""), key="invoice_biko", height=80)
        furikomi = st.text_area("振込先", value=h.get("振込先", ""), key="invoice_furikomi", height=80)


# ============================================================
# 3. 保管料（2期制）
# ============================================================
st.header("③ 保管料（2期制：15日・末日）")
st.caption("各種別について15日時点と末日時点の数量を入力すると、平均×単価で自動計算します。")

master = client.get("保管料マスタ", [])
storage_default = pd.DataFrame([
    {"種別名": m["種別名"], "15日数量": 0, "末日数量": 0,
     "単価": m["単価"], "出力品名": m["出力品名"]}
    for m in master
])
if storage_default.empty:
    storage_default = pd.DataFrame(
        columns=["種別名", "15日数量", "末日数量", "単価", "出力品名"])

storage_edited = st.data_editor(
    storage_default,
    num_rows="dynamic",
    use_container_width=True,
    key="invoice_storage_editor",
    column_config={
        "種別名": st.column_config.TextColumn("種別名", width="medium"),
        "15日数量": st.column_config.NumberColumn("15日数量", min_value=0, step=1),
        "末日数量": st.column_config.NumberColumn("末日数量", min_value=0, step=1),
        "単価": st.column_config.NumberColumn("単価", min_value=0, step=10),
        "出力品名": st.column_config.TextColumn("出力品名（MF品目名）", width="medium"),
    },
)

# 平均・金額を計算し、出力品名ごとに集計
storage_lines = {}   # 出力品名 -> 金額合計
storage_preview = []
for _, row in storage_edited.iterrows():
    name = str(row.get("種別名", "")).strip()
    if not name:
        continue
    q15 = float(row.get("15日数量") or 0)
    qend = float(row.get("末日数量") or 0)
    price = float(row.get("単価") or 0)
    out_name = str(row.get("出力品名", "")).strip() or "保管料"
    avg = (q15 + qend) / 2
    amount = round(avg * price)
    storage_preview.append({
        "種別名": name, "平均数量": avg, "単価": price,
        "金額": amount, "出力品名": out_name})
    storage_lines[out_name] = storage_lines.get(out_name, 0) + amount

if storage_preview:
    st.dataframe(pd.DataFrame(storage_preview), use_container_width=True, hide_index=True)


# ============================================================
# 4. イレギュラー・その他費目（手入力）
# ============================================================
st.header("④ その他費目（送料・作業料・値引き等の手入力）")
st.caption("Phase1では手入力です。送料・作業料の自動算出はPhase2以降で追加します。")

other_default = pd.DataFrame([
    {"品名": "送料", "単価": 0, "数量": 1},
    {"品名": "出荷作業料", "単価": 0, "数量": 1},
    {"品名": "資材費", "単価": 0, "数量": 1},
    {"品名": "受注作業料", "単価": 0, "数量": 1},
    {"品名": "[汎用]作業料", "単価": 0, "数量": 1},
    {"品名": "その他", "単価": 0, "数量": 1},
    {"品名": "値引き", "単価": 0, "数量": 1},
])
other_edited = st.data_editor(
    other_default,
    num_rows="dynamic",
    use_container_width=True,
    key="invoice_other_editor",
    column_config={
        "品名": st.column_config.TextColumn("品名", width="medium"),
        "単価": st.column_config.NumberColumn("単価（マイナス可）", step=10),
        "数量": st.column_config.NumberColumn("数量", step=0.25),
    },
)


# ============================================================
# 5. 品目を組み立ててプレビュー＆CSV出力
# ============================================================
st.header("⑤ 請求内容の確認とCSV出力")

items = []
# 保管料（出力品名ごとに1行、数量1・単価=合計金額）
for out_name, amount in storage_lines.items():
    if amount != 0:
        items.append({"品名": out_name, "単価": amount, "数量": 1, "金額": amount})
# その他費目
for _, row in other_edited.iterrows():
    name = str(row.get("品名", "")).strip()
    if not name:
        continue
    price = float(row.get("単価") or 0)
    qty = float(row.get("数量") or 0)
    amount = round(price * qty)
    if price == 0 and qty == 0:
        continue
    items.append({"品名": name, "単価": price, "数量": qty, "金額": amount})

if not items:
    st.info("品目がありません。保管料またはその他費目を入力してください。")
    st.stop()

subtotal, tax, total = mf_export.calc_totals(items)

# プレビュー（表示専用：HTML白背景で見やすく）
prev_df = pd.DataFrame([
    {"品名": it["品名"], "単価": f"{int(it['単価']):,}",
     "数量": it["数量"], "金額": f"{int(it['金額']):,}"}
    for it in items
])
st.dataframe(prev_df, use_container_width=True, hide_index=True)

mcol1, mcol2, mcol3 = st.columns(3)
mcol1.metric("小計", f"{subtotal:,} 円")
mcol2.metric("消費税(10%)", f"{tax:,} 円")
mcol3.metric("合計金額", f"{total:,} 円")

# CSV生成
header = {
    "取引先名称": st.session_state.get("invoice_corp", client["header"].get("取引先名称", "")),
    "件名": subject,
    "請求日": issue_date,
    "お支払期限": due_date,
    "請求書番号": inv_no,
    "売上計上日": sales_date,
    "取引先敬称": st.session_state.get("invoice_keisho", ""),
    "取引先郵便番号": st.session_state.get("invoice_zip", ""),
    "取引先都道府県": st.session_state.get("invoice_pref", ""),
    "取引先住所1": st.session_state.get("invoice_addr1", ""),
    "取引先住所2": st.session_state.get("invoice_addr2", ""),
    "自社担当者氏名": staff,
    "備考": st.session_state.get("invoice_biko", ""),
    "振込先": st.session_state.get("invoice_furikomi", ""),
}

st.subheader("CSVダウンロード")
enc_label = st.radio("文字コード", ["UTF-8(BOM付き)", "Shift-JIS(cp932)"],
                     horizontal=True, key="invoice_enc")
encoding = "cp932" if enc_label.startswith("Shift") else "utf-8-sig"
csv_bytes = mf_export.to_csv_bytes(header, items, encoding=encoding)
filename = f"MF請求書_{client_name}_{inv_no}.csv"

st.download_button(
    "MFクラウド取込用CSVをダウンロード",
    data=csv_bytes,
    file_name=filename,
    mime="text/csv",
    key="invoice_download",
)

# 任意：Driveバックアップ
with st.expander("Googleドライブにバックアップ保存（任意）", expanded=False):
    folder_id = st.text_input(
        "保存先フォルダID（請求書専用フォルダを指定）",
        value=st.secrets.get("INVOICE_GDRIVE_FOLDER_ID", ""),
        key="invoice_drive_folder",
        help="Secretsに INVOICE_GDRIVE_FOLDER_ID を設定すると自動入力されます。",
    )
    if st.button("Driveへバックアップ", key="invoice_drive_btn"):
        if not folder_id:
            st.error("保存先フォルダIDを入力してください。")
        else:
            try:
                fid = store.backup_to_drive(csv_bytes, filename, folder_id)
                st.success(f"Driveへ保存しました（ファイルID: {fid}）")
            except Exception as e:
                st.error(f"Drive保存に失敗しました: {e}")
