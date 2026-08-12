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

from lib.payable import app_init, matching, mf_csv, notion_payable as N

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
                 "受取人口座名", "顧客番号", "固定額", "除外フラグ", "ルール", "支払元銀行", "備考",
                 # MFクラウド会計 仕訳用（買掛未払CSV・総合振込仕訳帳CSV）
                 "借方勘定科目", "借方補助科目", "借方税区分",
                 "貸方勘定科目", "貸方補助科目", "貸方税区分", "摘要", "MF並び順"]
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
            "摘要": st.column_config.TextColumn(
                "摘要（MF仕訳）", help="MFの仕訳に出す摘要。空ならこの会社名を使います"),
            "MF並び順": st.column_config.NumberColumn(
                "MF並び順", help="MF買掛未払CSVの行の並び順（経理シートの順番）"),
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
st.markdown("### 📗 MFクラウド会計の勘定科目を取り込む")
st.caption("経理シートの『[マスタ]買掛未払.csv』を読み込み、摘要（＝取引先名）で突き合わせて、"
           "借方/貸方の勘定科目・補助科目・税区分・並び順をマスタに登録します。"
           "これが入っていないと『買掛未払CSV』を作れません。")
with st.expander("CSVを読み込んで取り込む", expanded=False):
    mfup = st.file_uploader("[マスタ]買掛未払.csv", type=["csv"], key="pm_mfup")
    if mfup is not None:
        try:
            st.session_state["pm_mf_rows"] = mf_csv.read_mf_master_csv(mfup.getvalue())
        except Exception as e:  # noqa: BLE001
            st.error(f"CSVを読めませんでした: {e}")
    mf_rows = st.session_state.get("pm_mf_rows") or []
    if mf_rows:
        look = matching.build_master_lookup(rows)
        # 『野中製作所』と『野中製作所(コンテナ30％)』は正規化すると同じキーになるため、
        # 会社名の完全一致を最優先で引く（別行の設定を上書きしない）。
        pairs = [(m, matching.lookup_master(look, m["摘要"])) for m in mf_rows]
        matched = [(m, t) for m, t in pairs if t]
        unmatched = [m for m, t in pairs if not t]
        conf = [m for m, t in matched if str(m.get("借方勘定科目", "")).strip()]
        st.write(f"CSV {len(mf_rows)}行／マスタ一致 **{len(matched)}件**"
                 f"（うち勘定科目あり {len(conf)}件）／未一致 **{len(unmatched)}件**")
        st.dataframe(pd.DataFrame([{
            "摘要(CSV)": m["摘要"], "マスタ会社名": t["会社名"],
            "借方": f"{m.get('借方勘定科目','')} / {m.get('借方補助科目','')}",
            "税区分": m.get("借方税区分", ""),
            "貸方": f"{m.get('貸方勘定科目','')} / {m.get('貸方補助科目','')}",
            "並び順": m["MF並び順"],
        } for m, t in matched]), use_container_width=True, height=260)

        # 未一致は候補から手動で紐付け（選ぶと摘要を別名として登録）
        manual = {}
        if unmatched:
            st.markdown("**未一致（表記が違う／マスタ未登録）**")
            for m in unmatched:
                u1, u2 = st.columns([2, 3])
                u1.write(f"・{m['摘要']}")
                try:
                    cands = matching.find_candidates(m["摘要"], list(rows))
                except Exception:  # noqa: BLE001
                    cands = []
                opts = ["（取り込まない）"] + cands + [r["会社名"] for r in rows
                                                if r["会社名"] not in cands]
                pick = u2.selectbox(f"紐づける取引先（{m['摘要']}）", opts,
                                    key=f"pm_mfmap_{m['MF並び順']}", label_visibility="collapsed")
                if pick != "（取り込まない）":
                    manual[m["MF並び順"]] = pick
            st.caption("※ 選んで取り込むと、CSVの摘要が『別名』に登録され次回から自動で一致します。"
                       "マスタに無い取引先は先に上の表で登録してください。")

        if st.button("📗 マスタに取り込む", type="primary", key="pm_mfimport"):
            done = skipped = 0
            for m, t in pairs:
                tgt = t
                if tgt is None:
                    pick = manual.get(m["MF並び順"])
                    if not pick:
                        skipped += 1
                        continue
                    tgt = matching.lookup_master(look, pick)
                    if not tgt:
                        skipped += 1
                        continue
                try:
                    N.update_master_fields(
                        db_ids, tgt["id"],
                        借方勘定科目=m.get("借方勘定科目", ""),
                        借方補助科目=m.get("借方補助科目", ""),
                        借方税区分=m.get("借方税区分", ""),
                        貸方勘定科目=m.get("貸方勘定科目", ""),
                        貸方補助科目=m.get("貸方補助科目", ""),
                        貸方税区分=m.get("貸方税区分", ""),
                        摘要=m.get("摘要", ""), MF並び順=m.get("MF並び順", ""))
                    if t is None:  # 手動で紐づけたものは表記ゆれを別名に学習させる
                        try:
                            N.add_alias_by_company(db_ids, tgt["会社名"], m["摘要"])
                        except Exception:  # noqa: BLE001
                            pass
                    done += 1
                except Exception as e:  # noqa: BLE001
                    st.error(f"{m['摘要']} の取り込みに失敗: {e}")
            st.session_state.pop("pm_rows", None)
            st.session_state["payable_master_nonce"] = \
                st.session_state.get("payable_master_nonce", 0) + 1
            st.success(f"{done}件を取り込みました（未紐付けのため見送り {skipped}件）。")
            st.rerun()

with st.expander("ℹ️ 使い方とseedについて", expanded=False):
    st.markdown(
        "- 初回アクセス時に `payable_master_seed.csv`（122社）を自動投入します。\n"
        "- **NE仕入先cd**：突合の最優先キー。発注データの仕入先cd（例 n001）を入れると確実に紐付きます。\n"
        "- **別名**：請求書上の表記ゆれを `;` 区切りで登録すると、AI読取の会社名と照合できます。\n"
        "- **除外フラグ**：`✓` を入れると振込CSVの対象から外れます（口座振替・現金等）。\n"
        "- 既存の請求書発行（請求_*）DBには一切影響しません。"
    )
