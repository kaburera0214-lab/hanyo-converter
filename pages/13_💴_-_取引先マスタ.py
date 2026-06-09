# -*- coding: utf-8 -*-
"""
取引先マスタ（買掛）

振込先口座・支払条件・NE仕入先cd・別名（請求書表記ゆれ）を管理する。
初回はpayable_master_seed.csv（122社）を自動投入。突合の名寄せ精度を上げるため、
NE仕入先cdと別名をここで紐付ける。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="取引先マスタ（買掛）", layout="wide")
st.title("💴 取引先マスタ（買掛）")
st.caption("振込先口座・支払条件・名寄せ（NE仕入先cd／別名）を管理します。")

from lib.payable import app_init, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

if st.button("🔄 最新に更新", key="pm_reload"):
    st.session_state.pop("pm_rows", None)

if "pm_rows" not in st.session_state:
    st.session_state["pm_rows"] = N.load_master(db_ids)
rows = st.session_state["pm_rows"]

st.markdown(f"### 登録済み {len(rows)}社")
kw = st.text_input("会社名で絞り込み（空欄で全件）", key="pm_kw").strip()
view = [r for r in rows if not kw or kw in r["会社名"]]

# 編集テーブル（名寄せ列を重視）
df = pd.DataFrame(view)
if df.empty:
    st.info("該当する取引先がありません。")
else:
    edit_cols = ["id", "会社名", "別名", "NE仕入先cd", "科目", "支払方法", "支払日",
                 "銀行", "銀行番号", "支店番号", "預金種目", "口座番号", "受取人口座名",
                 "顧客番号", "固定額", "除外フラグ", "備考"]
    for c in edit_cols:
        if c not in df.columns:
            df[c] = ""
    df = df[edit_cols]
    edited = st.data_editor(
        df, use_container_width=True, num_rows="dynamic", key="pm_editor",
        column_config={
            "id": st.column_config.TextColumn("id", disabled=True, width="small"),
            "別名": st.column_config.TextColumn("別名（請求書表記ゆれ。;区切り）"),
            "NE仕入先cd": st.column_config.TextColumn("NE仕入先cd"),
            "預金種目": st.column_config.SelectboxColumn("預金種目", options=["", "普通", "当座"]),
            "除外フラグ": st.column_config.TextColumn("除外", help="✓で振込CSV対象外"),
        },
    )

    if st.button("💾 変更を保存", type="primary", key="pm_save"):
        n = 0
        for _, r in edited.iterrows():
            rec = {k: ("" if pd.isna(r.get(k)) else r.get(k)) for k in edit_cols}
            if not str(rec["会社名"]).strip():
                continue
            try:
                N.upsert_master_row(db_ids, rec)
                n += 1
            except Exception as e:  # noqa: BLE001
                st.error(f"{rec['会社名']} の保存に失敗: {e}")
        st.session_state.pop("pm_rows", None)
        st.session_state["payable_master_nonce"] = st.session_state.get("payable_master_nonce", 0) + 1
        st.success(f"{n}件を保存しました。")
        st.rerun()

st.markdown("---")
with st.expander("ℹ️ 使い方とseedについて", expanded=False):
    st.markdown(
        "- 初回アクセス時に `payable_master_seed.csv`（122社）を自動投入します。\n"
        "- **NE仕入先cd**：突合の最優先キー。発注データの仕入先cd（例 n001）を入れると確実に紐付きます。\n"
        "- **別名**：請求書上の表記ゆれを `;` 区切りで登録すると、AI読取の会社名と照合できます。\n"
        "- **除外フラグ**：`✓` を入れると振込CSVの対象から外れます（口座振替・現金等）。\n"
        "- 既存の請求書発行（請求_*）DBには一切影響しません。"
    )
