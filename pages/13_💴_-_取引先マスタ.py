# -*- coding: utf-8 -*-
"""
取引先マスタ（買掛）

振込先口座・支払条件・NE仕入先cd・別名（請求書表記ゆれ）を管理する。
初回はpayable_master_seed.csv（122社）を自動投入。突合の名寄せ精度を上げるため、
NE仕入先cdと別名をここで紐付ける。
"""
import inspect

import streamlit as st
import pandas as pd

st.set_page_config(page_title="取引先マスタ（買掛）", layout="wide")

from lib.auth import require_role
require_role("payable")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("💴 取引先マスタ（買掛）")
st.caption("振込先口座・支払条件・名寄せ（NE仕入先cd／別名）を管理します。")

from lib.payable import app_init, matching, mf_csv, bank_master as BM, notion_payable as N

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

# ── 新規登録（用途に応じて必須項目をチェック） ──────────────
st.markdown("### ➕ 新規取引先を登録")
st.caption("楽天振込CSV・MF買掛未払CSVは、口座情報や勘定科目が揃っていないと出力できません。"
           "登録時に用途を選ぶと、必要な項目が未入力なら保存できないようにします。")
# 入力欄は原則1行1項目（縦並び）。必須/任意はラベルの色で区別する。
_HAS_NEW_OPT = "accept_new_options" in inspect.signature(st.selectbox).parameters


def _lbl(name, required=False):
    return f"**{name}** " + (":red[**必須**]" if required else ":gray[任意]")


def _master_values(field):
    """登録済みマスタにあるその項目の値（候補プルダウン用）。"""
    seen = []
    for r in rows:
        v = str(r.get(field, "") or "").strip()
        if v and v not in seen:
            seen.append(v)
    return sorted(seen)


def _combo(label, field, key, *, required=False, default="", extra=(), help=None):
    """
    既存マスタの値から選べて、新しい値も直接入力できる入力欄。
    Streamlitが accept_new_options に未対応の場合は「新しい値を入力」に切り替える方式。
    """
    opts = list(dict.fromkeys([*extra, *_master_values(field)]))
    if default and default not in opts:
        opts.insert(0, default)
    if _HAS_NEW_OPT:
        v = st.selectbox(_lbl(label, required), opts,
                         index=opts.index(default) if default in opts else None,
                         key=key, help=help, accept_new_options=True,
                         placeholder="選択、または新しい値を直接入力")
        return str(v or "").strip()
    NEW = "＋ 新しい値を入力"
    pick = st.selectbox(_lbl(label, required), [NEW] + opts,
                        index=(opts.index(default) + 1) if default in opts else 0,
                        key=key, help=help)
    if pick == NEW:
        return st.text_input(f"↳ {label}（新しい値）", key=f"{key}_new").strip()
    return pick


_new_msg = st.session_state.pop("pm_new_msg", None)
if _new_msg:
    st.success(_new_msg)
with st.expander("新しい取引先を追加する", expanded=False):
    st.markdown(":red[**必須**] は未入力だと登録できません。:gray[任意] は後からでも入力できます。")
    n_name = st.text_input(_lbl("会社名", True), key="pm_new_name",
                           placeholder="請求書に出てくる名称（例：野中製作所）")

    st.markdown("**この取引先の用途**（チェックした用途の項目が必須になります）")
    use_furikomi = st.checkbox("銀行振込する（楽天振込CSVの対象）", value=True, key="pm_new_use_f")
    use_mf = st.checkbox("MF会計に計上する（買掛未払CSV）", value=True, key="pm_new_use_mf")

    st.divider()
    st.markdown("#### 支払条件")
    n_kubun = st.selectbox(_lbl("支払区分"), ["銀行振込", "カード払い"], key="pm_new_kubun")
    n_houhou = _combo("支払方法", "支払方法", "pm_new_houhou", required=use_furikomi,
                      default="振込" if use_furikomi else "",
                      extra=["振込", "口座振替", "現金", "支払"])
    n_kamoku = _combo("科目", "科目", "pm_new_kamoku",
                      help="仕入／業務委託／荷造運賃 など。ダッシュボードの分類に使います")
    n_payday = _combo("支払日", "支払日", "pm_new_payday", extra=["末日", "27日", "都度"])
    n_alias = st.text_input(_lbl("別名（請求書の表記ゆれ。;区切り）"), key="pm_new_alias")
    n_necd = st.text_input(_lbl("NE仕入先cd"), key="pm_new_necd",
                           placeholder="例：n001（発注データとの突合キー）")

    if use_furikomi:
        st.divider()
        st.markdown("#### 振込先口座 :blue[（楽天振込CSVに必要）]")
        n_bank_no = st.text_input(_lbl("銀行番号（4桁）", True), key="pm_new_bankno")
        n_branch_no = st.text_input(_lbl("支店番号（3桁）", True), key="pm_new_branchno")
        # 番号から銀行名・支店名を自動解決して、打ち間違いをその場で気づけるようにする
        n_bank = BM.bank_name(n_bank_no) if n_bank_no.strip() else ""
        n_branch = BM.branch_name(n_bank_no, n_branch_no) if n_branch_no.strip() else ""
        if n_bank or n_branch:
            st.info(f"🏦 {n_bank or '（銀行名不明）'} {n_branch or ''}")
        elif n_bank_no.strip():
            st.warning("銀行番号・支店番号から金融機関を特定できません。番号をご確認ください。")
        n_shumoku = st.selectbox(_lbl("預金種目", True), ["普通", "当座"], key="pm_new_shumoku")
        n_acc = st.text_input(_lbl("口座番号", True), key="pm_new_acc")
        n_holder = st.text_input(_lbl("受取人口座名", True), key="pm_new_holder",
                                 placeholder="例：カ）ノナカ　セイサクシヨ",
                                 help="楽天CSVにこのまま出力されます")
    else:
        n_bank_no = n_branch_no = n_acc = n_holder = n_bank = n_branch = ""
        n_shumoku = ""

    if use_mf:
        st.divider()
        st.markdown("#### MF会計の仕訳 :blue[（買掛未払CSVに必要）]")
        n_kari = _combo("借方勘定科目", "借方勘定科目", "pm_new_kari", required=True,
                        help="仕入高／業務委託料／荷造運賃 など")
        n_kari_h = _combo("借方補助科目", "借方補助科目", "pm_new_karih")
        n_kari_t = _combo("借方税区分", "借方税区分", "pm_new_karit", required=True,
                          default="課税仕入 10%", extra=["課税仕入 10%", "課税仕入 8%", "対象外"])
        n_kashi = _combo("貸方勘定科目", "貸方勘定科目", "pm_new_kashi", required=True,
                         default="買掛金", extra=["買掛金", "未払金"],
                         help="仕入なら買掛金、経費なら未払金")
        n_kashi_h = _combo("貸方補助科目", "貸方補助科目", "pm_new_kashih")
        n_kashi_t = _combo("貸方税区分", "貸方税区分", "pm_new_kashit", required=True,
                           default="対象外", extra=["対象外", "課税仕入 10%"])
        n_tekiyo = st.text_input(_lbl("摘要"), key="pm_new_tekiyo",
                                 placeholder="空欄なら会社名を使います")
    else:
        n_kari = n_kari_h = n_kari_t = n_kashi = n_kashi_h = n_kashi_t = n_tekiyo = ""

    st.divider()
    n_biko = st.text_input(_lbl("備考"), key="pm_new_biko")

    if st.button("➕ この内容で登録する", type="primary", key="pm_new_save"):
        errors = []
        if not n_name.strip():
            errors.append("会社名")
        if use_furikomi:
            if not str(n_houhou).strip():
                errors.append("支払方法")
            for label, v in [("銀行番号", n_bank_no), ("支店番号", n_branch_no),
                             ("口座番号", n_acc), ("受取人口座名", n_holder)]:
                if not str(v).strip():
                    errors.append(label)
        if use_mf:
            for label, v in [("借方勘定科目", n_kari), ("借方税区分", n_kari_t),
                             ("貸方勘定科目", n_kashi), ("貸方税区分", n_kashi_t)]:
                if not str(v).strip():
                    errors.append(label)
        dup = matching.lookup_master(matching.build_master_lookup(rows), n_name) \
            if n_name.strip() else None
        if errors:
            st.error("次の必須項目が未入力です： " + "、".join(errors))
        elif dup:
            st.error(f"『{dup['会社名']}』として既に登録されています。"
                     "表記ゆれなら、その行の『別名』に追記してください。")
        else:
            rec = {
                "会社名": n_name.strip(), "別名": n_alias.strip(), "NE仕入先cd": n_necd.strip(),
                "支払区分": n_kubun, "科目": n_kamoku.strip(), "支払方法": str(n_houhou).strip(),
                "支払日": n_payday.strip(),
                "銀行": n_bank, "支店": n_branch,
                "銀行番号": n_bank_no.strip(), "支店番号": n_branch_no.strip(),
                "預金種目": n_shumoku, "口座番号": n_acc.strip(),
                "受取人口座名": n_holder.strip(), "備考": n_biko.strip(),
                "借方勘定科目": n_kari.strip(), "借方補助科目": n_kari_h.strip(),
                "借方税区分": n_kari_t.strip(), "貸方勘定科目": n_kashi.strip(),
                "貸方補助科目": n_kashi_h.strip(), "貸方税区分": n_kashi_t.strip(),
                "摘要": (n_tekiyo.strip() or n_name.strip()) if use_mf else "",
            }
            try:
                N.upsert_master_row(db_ids, rec)
                st.session_state.pop("pm_rows", None)
                st.session_state["payable_master_nonce"] = \
                    st.session_state.get("payable_master_nonce", 0) + 1
                # 入力欄を空に戻す(続けて登録できるように)
                for k in [k for k in list(st.session_state.keys())
                          if str(k).startswith("pm_new_")]:
                    del st.session_state[k]
                st.session_state["pm_new_msg"] = f"『{rec['会社名']}』を登録しました。"
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"登録に失敗しました: {e}")

st.markdown(f"### 登録済み {len(rows)}社")
st.caption("既存の内容はここで直接編集できます（新規追加は上の登録フォームから）。")
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
    # 新規行はここでは追加しない（必須項目チェックのため上の登録フォームに集約）
    edited = st.data_editor(
        df, use_container_width=True, num_rows="fixed", key="pm_editor",
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

# ── 削除 ─────────────────────────────────────────────
st.markdown("### 🗑️ 取引先を削除")
_del_msg = st.session_state.pop("pm_del_msg", None)
if _del_msg:
    st.success(_del_msg)
with st.expander("登録済みの取引先を削除する", expanded=False):
    _names = [r["会社名"] for r in rows if r.get("id")]
    dels = st.multiselect("削除する取引先（複数選べます）", _names, key="pm_del_sel")
    if dels:
        _targets = [r for r in rows if r["会社名"] in dels and r.get("id")]
        st.dataframe(pd.DataFrame([{
            "会社名": r["会社名"], "科目": r.get("科目", ""), "支払方法": r.get("支払方法", ""),
            "銀行": f"{r.get('銀行','')} {r.get('支店','')}", "口座番号": r.get("口座番号", ""),
            "借方勘定科目": r.get("借方勘定科目", ""), "NE仕入先cd": r.get("NE仕入先cd", ""),
        } for r in _targets]), use_container_width=True)
        st.warning("⚠️ 削除すると元に戻せません。過去の請求書レコードは残りますが、"
                   "この取引先とのマスタ照合（口座・勘定科目・突合の名寄せ）が外れます。"
                   "一時的に振込対象から外したいだけなら、『除外フラグ』に ✓ を入れてください。")
        ok_del = st.checkbox("内容を確認しました", key="pm_del_ok")
        if st.button(f"🗑️ 選択した{len(_targets)}社を削除する", type="primary",
                     disabled=not ok_del, key="pm_del_btn"):
            n = 0
            for r in _targets:
                try:
                    N.delete_master_row(db_ids, r["id"])
                    n += 1
                except Exception as e:  # noqa: BLE001
                    st.error(f"{r['会社名']} の削除に失敗: {e}")
            st.session_state.pop("pm_rows", None)
            st.session_state.pop("pm_del_sel", None)
            st.session_state.pop("pm_del_ok", None)
            st.session_state["payable_master_nonce"] = \
                st.session_state.get("payable_master_nonce", 0) + 1
            st.session_state["pm_del_msg"] = f"{n}社を削除しました。"
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
