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

from lib.auth import require_role
require_role("payable")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("💴 取引先マスタ（買掛）")
st.caption("振込先口座・支払条件・名寄せ（NE仕入先cd／別名）を管理します。")

from lib.payable import app_init, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

rc1, rc2 = st.columns([1, 3])
if rc1.button("🔄 最新に更新", key="pm_reload"):
    st.session_state.pop("pm_rows", None)
if rc2.button("🧹 重複レコードを整理", key="pm_dedupe",
              help="同一→統合／差分あるが項目かぶりなし→結合／項目競合→残す"):
    with st.spinner("重複を整理中…"):
        rep = N.dedupe_master(db_ids)
    st.session_state.pop("pm_rows", None)
    st.session_state["payable_master_nonce"] = st.session_state.get("payable_master_nonce", 0) + 1
    st.success(f"統合{rep['統合']}件・結合{rep['結合']}件・競合保留{rep['競合保留']}件"
               f"（{rep['削除']}レコード削除）")
    if rep["詳細"]:
        with st.expander("整理の詳細", expanded=True):
            for line in rep["詳細"]:
                st.write("- " + line)

if st.button("🏦 銀行名・支店名を番号から補完", key="pm_enrich",
             help="銀行番号・支店番号から銀行名/支店名を埋め、既存の『銀行』(楽天等)は支払元銀行へ退避"):
    with st.spinner("補完中…"):
        rep = N.enrich_bank_names(db_ids)
    st.session_state.pop("pm_rows", None)
    st.session_state["payable_master_nonce"] = st.session_state.get("payable_master_nonce", 0) + 1
    st.success(f"{rep['更新']}社の銀行名・支店名を補完しました。")

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
    edit_cols = ["id", "会社名", "別名", "NE仕入先cd", "支払区分", "科目", "支払方法",
                 "支払日", "銀行", "支店", "銀行番号", "支店番号", "預金種目", "口座番号",
                 "受取人口座名", "顧客番号", "固定額", "除外フラグ", "ルール", "支払元銀行", "備考"]
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
            "支払区分": st.column_config.SelectboxColumn(
                "支払区分", options=["銀行振込", "カード払い"],
                help="カード払いは楽天振込CSVの対象外"),
            "ルール": st.column_config.TextColumn(
                "ルール", help="『100万超で振込』のように書くと、ダッシュボードで該当月にアラート"),
            "預金種目": st.column_config.SelectboxColumn("預金種目", options=["", "普通", "当座"]),
            "除外フラグ": st.column_config.TextColumn("除外", help="✓で振込CSV対象外"),
        },
    )

    if st.button("💾 変更を保存", type="primary", key="pm_save"):
        # 変更行・新規行だけ保存(毎回全件更新を避ける→高速・失敗しにくい)
        orig = {r["id"]: r for r in st.session_state.get("pm_rows", []) if r.get("id")}
        created = updated = 0
        for _, r in edited.iterrows():
            rec = {k: ("" if pd.isna(r.get(k)) else r.get(k)) for k in edit_cols}
            if not str(rec["会社名"]).strip():
                continue
            rid = str(rec.get("id") or "").strip()
            is_new = rid.lower() in ("", "nan", "none")
            try:
                if is_new:
                    rec["id"] = ""
                    N.upsert_master_row(db_ids, rec)
                    created += 1
                else:
                    o = orig.get(rid, {})
                    changed = any(str(rec.get(k, "")).strip() != str(o.get(k, "")).strip()
                                  for k in edit_cols if k != "id")
                    if changed:
                        N.upsert_master_row(db_ids, rec)
                        updated += 1
            except Exception as e:  # noqa: BLE001
                st.error(f"{rec['会社名']} の保存に失敗: {e}")
        st.session_state.pop("pm_rows", None)
        st.session_state["payable_master_nonce"] = st.session_state.get("payable_master_nonce", 0) + 1
        st.success(f"新規{created}件・更新{updated}件を保存しました。")
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
