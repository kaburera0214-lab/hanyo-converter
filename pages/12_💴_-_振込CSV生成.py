# -*- coding: utf-8 -*-
"""
振込CSV生成（買掛）

ステータスが「確認済」の請求書のみを抽出し、取引先マスタの口座情報を使って
楽天銀行 総合振込インポートCSV(Shift-JIS)を生成・自動ダウンロードする。
口座未登録・除外フラグの取引先は対象外。生成内容は振込履歴に保存。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="振込CSV生成", layout="wide")

from lib.auth import require_role
require_role("payable")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("💴 振込CSV生成（楽天銀行 総合振込）")
st.caption("『確認済』の請求書から楽天銀行インポート用CSVを作成します。")

from lib.payable import app_init, matching, rakuten_csv, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 1])
target_ym = c1.text_input("対象月（例 2026-05）", value=st.session_state.get("payable_target_ym", ""),
                          key="csv_ym")
st.session_state["payable_target_ym"] = target_ym
exec_date = c2.text_input("振込実行日（MMDD 例 0430）", value=st.session_state.get("payable_exec", ""),
                          key="csv_exec")
st.session_state["payable_exec"] = exec_date
if c3.button("🔄 再読込", key="csv_reload"):
    st.session_state.pop("csv_invoices", None)

if "csv_invoices" not in st.session_state:
    st.session_state["csv_invoices"] = N.load_invoices(db_ids, target_ym=target_ym, status="確認済")
confirmed = st.session_state["csv_invoices"]

master_rows = N.load_master(db_ids)
look = matching.build_master_lookup(master_rows)

# 確認済請求書をマスタ口座に結合
records, skipped = [], []
for inv in confirmed:
    m = look["by_norm"].get(matching.normalize_name(inv["会社名"]))
    amount = int(inv["当月請求額"] or 0)
    if not m:
        skipped.append((inv["会社名"], "マスタ未登録"))
        continue
    if str(m.get("支払区分", "")).strip() == "カード払い":
        skipped.append((inv["会社名"], "カード払い（振込CSV対象外）"))
        continue
    if any(k in str(m.get("支払方法", "")) for k in ("口座振替", "現金")):
        skipped.append((inv["会社名"], f"{m.get('支払方法')}（振込CSV対象外）"))
        continue
    if str(m.get("除外フラグ", "")).strip() in ("✓", "○", "1"):
        skipped.append((inv["会社名"], "除外フラグ"))
        continue
    if not (m.get("銀行番号") and m.get("支店番号") and m.get("口座番号")):
        skipped.append((inv["会社名"], "口座情報が不足"))
        continue
    if amount <= 0:
        skipped.append((inv["会社名"], "金額が0以下"))
        continue
    records.append({
        "会社名": m["会社名"],
        "銀行番号": m["銀行番号"], "支店番号": m["支店番号"],
        "預金種目": m.get("預金種目", "普通"), "口座番号": m["口座番号"],
        "受取人口座名": m.get("受取人口座名", ""), "金額": amount,
    })

st.markdown(f"### 振込対象：{len(records)}件")
if records:
    st.dataframe(pd.DataFrame([{
        "会社名": r["会社名"], "銀行番号": str(r["銀行番号"]).zfill(4),
        "支店": str(r["支店番号"]).zfill(3), "種目": r["預金種目"],
        "口座番号": str(r["口座番号"]).zfill(7), "口座名": r["受取人口座名"],
        "金額": r["金額"],
    } for r in records]), use_container_width=True)
    st.markdown(f"**合計 {sum(r['金額'] for r in records):,} 円**")
else:
    st.info("『確認済』ステータスの振込対象がありません。『突合確認』でステータスを確認済にしてください。")

if skipped:
    with st.expander(f"⚠️ 対象外 {len(skipped)}件（要確認）", expanded=True):
        for name, reason in skipped:
            st.write(f"- {name}： {reason}")

st.markdown("---")
disabled = not records or not exec_date.strip()
if st.button("📥 楽天CSVを生成してダウンロード", type="primary", disabled=disabled,
             key="csv_gen"):
    csv_bytes = rakuten_csv.build_csv_bytes(records, exec_date.strip())
    csv_name = f"楽天総合振込_{target_ym}_{exec_date.strip()}.csv"
    try:
        N.save_transfer_history(db_ids, 実行日=exec_date.strip(), 対象月=target_ym, records=records)
    except Exception as e:  # noqa: BLE001
        st.warning(f"振込履歴の保存に失敗（CSVは生成済）: {e}")
    st.session_state["csv_payload"] = (csv_name, csv_bytes)
    st.session_state["csv_autodl"] = True
    st.rerun()

if st.session_state.pop("csv_autodl", False):
    import base64 as _b64
    import streamlit.components.v1 as _components
    name, data = st.session_state["csv_payload"]
    b64 = _b64.b64encode(data).decode()
    _components.html(f"""
    <script>
    var a=document.createElement('a');
    a.href='data:text/csv;base64,{b64}'; a.download={name!r};
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    </script>
    """, height=0)
    st.success("楽天CSVのダウンロードを開始しました。")

if st.session_state.get("csv_payload"):
    name, data = st.session_state["csv_payload"]
    with st.expander("ダウンロードされない場合（手動DL）", expanded=False):
        st.download_button("⬇️ 楽天総合振込CSV", data=data, file_name=name,
                           mime="text/csv", key="csv_dl_manual")
