# -*- coding: utf-8 -*-
"""
請求書取込（買掛）

取引先から届いた請求書(PDF/画像)をアップロードし、Claude APIで会社名・当月請求額・
振込先口座・支払期日などを抽出。取引先マスタの口座と照合し、相違があれば
口座変更の可能性として注意喚起。確認のうえ「読取済」でNotionに保存する。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="請求書取込", layout="wide")

from lib.auth import require_role
require_role("payable")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("💴 請求書取込（買掛）")
st.caption("請求書PDF/画像をAIで読み取り、口座マスタと照合して登録します。")

from lib.payable import app_init, extract, matching, bank_master as BM, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()


@st.cache_data(ttl=300, show_spinner=False)
def _load_master_cached(_nonce):
    return N.load_master(db_ids)


def _master():
    return _load_master_cached(st.session_state.get("payable_master_nonce", 0))


master_rows = _master()
look = matching.build_master_lookup(master_rows)


def _candidate_accounts(data):
    """請求書から読み取った口座候補 [(銀行,支店,口座番号)] を作る(複数口座対応)。"""
    cands = []
    p = (data.get("振込先銀行", ""), data.get("振込先支店", ""), data.get("口座番号", ""))
    if p[2] or p[0]:
        cands.append(p)
    for a in data.get("口座一覧", []) or []:
        if isinstance(a, dict):
            cands.append((a.get("bank", ""), a.get("branch", ""), a.get("account_number", "")))
    return cands


def _account_mismatch(m, data):
    """
    マスタ口座と請求書記載口座の相違を判定。
    複数口座が記載されていても、マスタ口座がそのいずれかに含まれれば一致(警告なし)。
    """
    if not m:
        return False, ""
    cands = _candidate_accounts(data)
    acc_m = BM.digits(m.get("口座番号", "")).lstrip("0")
    cand_accs = [BM.digits(c[2]).lstrip("0") for c in cands if BM.digits(c[2])]
    if acc_m and cand_accs:
        if acc_m in cand_accs:
            return False, ""
        listed = " / ".join(c[2] for c in cands if c[2])
        return True, f"マスタ口座 {m.get('口座番号','')} が請求書記載口座（{listed}）に見つかりません"
    bank_m = BM.normalize_bank(m.get("銀行", ""))
    cand_banks = [BM.normalize_bank(c[0]) for c in cands if c[0]]
    if bank_m and cand_banks and not any(
            bank_m == b or bank_m in b or b in bank_m for b in cand_banks):
        return True, f"銀行 マスタ『{m.get('銀行','')}』が請求書記載銀行に見つかりません"
    return False, ""


def _preview(fname, idx):
    """アップロードファイルのプレビュー。端末依存を避けるためPDFは画像化して表示。"""
    fb = st.session_state.get("payable_filebytes", {}).get(fname)
    if not fb:
        st.caption("（プレビュー用データがありません。再アップロードで表示できます）")
        return
    imgs = extract.render_preview_images(fb, fname)
    st.download_button("⬇️ 元ファイルをダウンロード", data=fb, file_name=fname.split("/")[-1],
                       key=f"pay_dl_{idx}")
    if imgs:
        for img in imgs:
            st.image(img, use_container_width=True)
    else:
        st.info("この環境ではプレビュー画像を生成できませんでした。ダウンロードして確認してください。")


# 対象月
c1, c2 = st.columns([1, 3])
target_ym = c1.text_input("対象月（例 2026-05）", value=st.session_state.get("payable_target_ym", ""),
                          key="payable_ym_in")
st.session_state["payable_target_ym"] = target_ym

st.markdown("### 1. 請求書をアップロード")
# アップローダはキー番号を変えることでクリア(リセット)できる
up_key = st.session_state.get("payable_up_key", 0)
files = st.file_uploader("PDF / 画像 / ZIP（複数可・ZIPは中身を一括展開）",
                         type=["pdf", "png", "jpg", "jpeg", "webp", "zip"],
                         accept_multiple_files=True, key=f"payable_uploader_{up_key}")

entries = extract.iter_files_from_uploads(files)
uc1, uc2 = st.columns([1, 3])
if files:
    uc2.caption(f"読取対象ファイル：{len(entries)}件（ZIPは展開後の件数）")
if uc1.button("🗑️ 読込結果をクリア", key="payable_clear"):
    for k in ["payable_extracted", "payable_filebytes"]:
        st.session_state.pop(k, None)
    st.session_state["payable_up_key"] = up_key + 1  # アップローダを空に
    st.rerun()

use_sonnet = st.checkbox("読みにくい請求書（スキャン等）はSonnetで精度優先", value=False,
                         key="payable_sonnet")

if entries and st.button("🤖 AIで読み取る", type="primary", key="payable_extract_btn"):
    results = []
    filebytes = {}
    prog = st.progress(0.0)
    for i, (fname, fbytes) in enumerate(entries):
        data = (extract.extract_invoice(fbytes, fname, model=extract.SONNET)
                if use_sonnet else extract.extract_with_fallback(fbytes, fname))
        data["_file"] = fname
        data["_ai会社名"] = data.get("会社名", "")  # 学習用:AIの読取値を保持
        results.append(data)
        filebytes[fname] = fbytes
        prog.progress((i + 1) / len(entries))
    st.session_state["payable_extracted"] = results
    st.session_state["payable_filebytes"] = filebytes
    prog.empty()

# ── 登録済みデータの管理（削除）：アップロードの有無に関わらず常に表示 ──
st.markdown("### 登録済みの請求書（対象月）")
if not target_ym.strip():
    st.caption("対象月を入力すると、登録済みデータの確認・削除ができます。")
else:
    rc1, rc2 = st.columns([1, 4])
    if rc1.button("🔄 登録済みを再読込", key="payable_reg_reload"):
        st.session_state.pop("payable_registered", None)
    if (st.session_state.get("payable_reg_ym") != target_ym
            or "payable_registered" not in st.session_state):
        st.session_state["payable_registered"] = N.load_invoices(db_ids, target_ym=target_ym)
        st.session_state["payable_reg_ym"] = target_ym
    regs = st.session_state["payable_registered"]
    if not regs:
        rc2.caption(f"対象月 {target_ym} の登録済みデータはありません。")
    else:
        rc2.caption(f"対象月 {target_ym}：{len(regs)}件　各行を開くと読取内容の確認・修正・"
                    "ステータス更新ができます。")

        _FREE = ("読取済", "保留")  # この2つは確認なしで削除可
        _STAT2 = ["保留", "読取済", "確認済"]

        def _drop_caches():
            st.session_state.pop("payable_registered", None)
            st.session_state.pop("match_invoices", None)

        # 保留レコードを、アップロード中のファイル(ファイル名一致)でAI再読取
        holds = [r for r in regs if r["ステータス"] == "保留"]
        name2bytes = {n: b for n, b in entries}
        if holds:
            matched = [r for r in holds if r.get("ファイルリンク") in name2bytes]
            hr1, hr2 = st.columns([2, 4])
            if hr1.button(f"🤖 保留をAI再読取（一致 {len(matched)}/保留 {len(holds)}）",
                          disabled=not matched, key="reg_reread"):
                prog = st.progress(0.0)
                for i, r in enumerate(matched):
                    fb = name2bytes[r["ファイルリンク"]]
                    d = (extract.extract_invoice(fb, r["ファイルリンク"], model=extract.SONNET)
                         if use_sonnet else extract.extract_with_fallback(fb, r["ファイルリンク"]))
                    if not d.get("_error"):
                        comp = d.get("会社名", "")
                        mm = look["by_norm"].get(matching.normalize_name(comp))
                        mism, _ = _account_mismatch(mm, d)
                        N.update_invoice_fields(
                            db_ids, r["id"], 会社名=comp,
                            当月税抜額=d.get("当月税抜額", 0), 当月請求額=d.get("当月請求額", 0),
                            今回請求額=d.get("今回請求額", 0), 前月繰越額=d.get("前月繰越額", 0),
                            税内訳=d.get("税内訳", ""), 抽出_銀行=d.get("振込先銀行", ""),
                            抽出_支店=d.get("振込先支店", ""), 抽出_預金種目=d.get("預金種目", ""),
                            抽出_口座番号=d.get("口座番号", ""), 抽出_口座名義=d.get("口座名義", ""),
                            口座相違フラグ=mism, 抽出メモ=d.get("信頼度メモ", ""),
                            請求日=d.get("請求日", ""), 支払期日=d.get("支払期日", ""))
                    prog.progress((i + 1) / len(matched))
                _drop_caches()
                st.success(f"{len(matched)}件を再読取しました。")
                st.rerun()
            hr2.caption("※ 上の『1.アップロード』に元ファイル(ZIP可)を入れると、ファイル名一致で"
                        "保留だけ再読取します（「AIで読み取る」は押さなくてOK）。")

        # 全選択 + まとめて削除
        select_all = st.checkbox("全選択", key="reg_selall")
        sel = (regs if select_all
               else [inv for inv in regs if st.session_state.get(f"regsel_{inv['id']}")])
        locked = [o for o in sel if o["ステータス"] not in _FREE]
        with st.popover(f"🗑️ 選択をまとめて削除（{len(sel)}）", disabled=not sel):
            st.caption("削除対象：" + "、".join(o["会社名"] for o in sel))
            if locked:
                st.warning(f"⚠️ 『読取済/保留』以外が{len(locked)}件含まれます。"
                           "必ず請求書を確認してください。削除は取り消せません。")
                ok_bulk = st.checkbox("請求書を確認しました", key="regbulk_ok")
            else:
                st.info("すべて『読取済/保留』のため、そのまま削除できます。")
                ok_bulk = True
            if st.button("選択を削除する", type="primary", disabled=not ok_bulk, key="regbulk_btn"):
                for o in sel:
                    N.delete_invoice(db_ids, o["id"])
                _drop_caches()
                st.rerun()

        for inv in regs:
            issue = (inv["ステータス"] == "保留") or inv.get("口座相違フラグ")
            head = (f"{'⚠️' if issue else '✅'} {inv['会社名']}｜{inv['ステータス']}"
                    f"｜突合:{inv.get('突合状態','')}"
                    f"｜税抜{int(inv.get('当月税抜額') or 0):,}/税込{int(inv.get('当月請求額') or 0):,}円")
            with st.expander(head, expanded=issue):
                st.checkbox("まとめ削除に選択", key=f"regsel_{inv['id']}")
                # AI読取内容の表示
                st.markdown(
                    f"- 振込先(抽出): {inv.get('抽出_銀行','') or '—'} {inv.get('抽出_支店','')} "
                    f"{inv.get('抽出_預金種目','')} {inv.get('抽出_口座番号','')}"
                    f"（名義 {inv.get('抽出_口座名義','') or '—'}）\n"
                    f"- 今回請求(繰越込) {int(inv.get('今回請求額') or 0):,} / "
                    f"前月繰越 {int(inv.get('前月繰越額') or 0):,}　税内訳: {inv.get('税内訳','') or '—'}\n"
                    f"- 突合: NE発注 {inv.get('NE合算額')} ＋送料 {inv.get('NE送料')} / 差額 {inv.get('差額')}\n"
                    f"- 請求日 {inv.get('請求日','') or '—'} / 支払期日 {inv.get('支払期日','') or '—'}\n"
                    f"- ファイル: {inv.get('ファイルリンク','')}\n"
                    f"- AIメモ: {inv.get('抽出メモ','') or '—'}")
                if inv.get("口座相違フラグ"):
                    st.error("⚠️ 口座変更の可能性（請求書の口座がマスタと不一致）")
                if st.toggle("📄 プレビュー表示", key=f"regprev_{inv['id']}"):
                    _preview(inv.get("ファイルリンク", ""), f"reg_{inv['id']}")

                # 修正・ステータス更新
                ecomp = st.text_input("会社名（修正可）", value=inv["会社名"],
                                      key=f"regcomp_{inv['id']}")
                m = look["by_norm"].get(matching.normalize_name(ecomp))
                if not m:
                    try:
                        cands = matching.find_candidates(ecomp, list(master_rows))
                    except Exception:  # noqa: BLE001
                        cands = []
                    if cands:
                        pk = st.selectbox("候補から選択（部分一致）", ["（選択しない）"] + cands,
                                          key=f"regcand_{inv['id']}")
                        if pk != "（選択しない）":
                            ecomp = pk
                            m = look["by_norm"].get(matching.normalize_name(ecomp))
                    if not m:
                        st.warning("会社名の不一致の可能性、または新規取引先の可能性があります。")
                if m:
                    st.success(f"マスタ照合: {m['会社名']}（{m.get('銀行','')} {m.get('支店','')} "
                               f"{m.get('預金種目','')} {m.get('口座番号','')}）")
                e1, e2, e3 = st.columns(3)
                eex = e1.number_input("当月税抜額", value=int(inv.get('当月税抜額') or 0),
                                      step=1, key=f"regex_{inv['id']}")
                einc = e2.number_input("当月請求額(税込)", value=int(inv.get('当月請求額') or 0),
                                       step=1, key=f"reginc_{inv['id']}")
                estat = e3.selectbox("ステータス", _STAT2,
                                     index=_STAT2.index(inv["ステータス"])
                                     if inv["ステータス"] in _STAT2 else 0,
                                     key=f"regstat_{inv['id']}")
                cur_skip = (inv.get("突合状態") == "対象外")
                eskip = st.checkbox("このファイルは突合しない（対象外。内訳・重複など）",
                                    value=cur_skip, key=f"regskip_{inv['id']}")
                bb1, bb2 = st.columns([1, 1])
                if bb1.button("💾 保存して反映", key=f"regsave_{inv['id']}"):
                    pdata = {"振込先銀行": inv.get('抽出_銀行', ''),
                             "振込先支店": inv.get('抽出_支店', ''),
                             "口座番号": inv.get('抽出_口座番号', ''), "口座一覧": []}
                    mism, _ = _account_mismatch(m, pdata)
                    fields = dict(会社名=ecomp, 当月税抜額=eex, 当月請求額=einc,
                                  ステータス=estat, 口座相違フラグ=mism)
                    # 突合しないの切替時のみ突合状態を変更(既存の突合結果を壊さない)
                    if eskip != cur_skip:
                        fields["突合対象"] = not eskip
                        fields["突合状態"] = "対象外" if eskip else "未突合"
                    N.update_invoice_fields(db_ids, inv["id"], **fields)
                    if m and matching.normalize_name(inv["会社名"]) != matching.normalize_name(ecomp):
                        try:
                            N.add_alias_by_company(db_ids, ecomp, inv["会社名"])
                        except Exception:  # noqa: BLE001
                            pass
                    _drop_caches()
                    st.session_state["payable_master_nonce"] = \
                        st.session_state.get("payable_master_nonce", 0) + 1
                    st.toast("保存しました")
                    st.rerun()
                if inv["ステータス"] in _FREE:
                    if bb2.button("🗑️削除", key=f"regdel_{inv['id']}"):
                        N.delete_invoice(db_ids, inv["id"])
                        _drop_caches()
                        st.rerun()
                else:
                    with bb2.popover("🗑️削除"):
                        st.warning("⚠️ 『読取済/保留』以降のステータスです。請求書を確認してから。")
                        ok = st.checkbox("請求書を確認しました", key=f"regdelok_{inv['id']}")
                        if st.button("削除する", type="primary", disabled=not ok,
                                     key=f"regdelbtn_{inv['id']}"):
                            N.delete_invoice(db_ids, inv["id"])
                            _drop_caches()
                            st.rerun()

st.markdown("---")
results = st.session_state.get("payable_extracted", [])
if not results:
    st.info("ファイルをアップロードして「AIで読み取る」を押してください。")
    st.stop()


st.markdown("### 2. 読取結果の確認")
st.caption("内容を確認し、必要なら金額・会社名を修正してから登録してください。")

def _file_issue(data):
    """このファイルに警告/エラーがあるか(アコーディオン展開判定)。
    複数口座でもマスタ口座が一致していれば警告にしない。"""
    if data.get("_error"):
        return True
    m0 = look["by_norm"].get(matching.normalize_name(data.get("会社名", "")))
    mism0, _ = _account_mismatch(m0, data)
    return (m0 is None) or mism0


def _base_row(data, fn, comp, cur, cur_ex, tot, carry, tax_bd, bill, due, cat, mism,
              status, taikai, match_state):
    """保存用の行dictを組み立てる。"""
    return {
        "会社名": comp, "当月請求額": cur, "当月税抜額": cur_ex,
        "今回請求額": tot, "前月繰越額": carry,
        "消費税額": data.get("消費税額", 0), "税内訳": tax_bd,
        "軽減税率": data.get("軽減税率", False),
        "請求日": bill, "支払期日": due, "カテゴリ": cat,
        "抽出_銀行": data.get("振込先銀行", ""), "抽出_支店": data.get("振込先支店", ""),
        "抽出_預金種目": data.get("預金種目", ""), "抽出_口座番号": data.get("口座番号", ""),
        "抽出_口座名義": data.get("口座名義", ""),
        "口座相違フラグ": mism,
        "抽出メモ": data.get("信頼度メモ", ""),
        "ファイルリンク": fn,
        "ステータス": status, "突合状態": match_state, "突合対象": taikai,
        "対象月": target_ym,
        "_ai会社名": data.get("_ai会社名", ""),
    }


def _render_file(idx, data):
    """1ファイル分の読取結果UIを描画し、rows_for_saveへ追記する。"""
    fn = data.get("_file", f"file{idx}")
    if data.get("_error"):
        st.error(data["_error"])
        return
    hc1, hc2 = st.columns([3, 2])
    skip = hc1.checkbox("このファイルは突合しない（内訳・重複など。データは保持されます）",
                        key=f"pay_skip_{idx}")
    show_prev = hc2.toggle("📄 プレビュー表示", key=f"pay_prev_{idx}")
    if show_prev:
        _preview(fn, idx)
    if skip:
        st.caption("→ 突合対象外として『保留』で保持します（後でNG掘り下げに使えます）。")
        rows_for_save.append(_base_row(
            data, fn, comp=data.get("会社名", ""),
            cur=int(data.get("当月請求額", 0) or 0), cur_ex=int(data.get("当月税抜額", 0) or 0),
            tot=int(data.get("今回請求額", 0) or 0), carry=int(data.get("前月繰越額", 0) or 0),
            tax_bd=data.get("税内訳", ""), bill=data.get("請求日", ""),
            due=data.get("支払期日", ""), cat="", mism=False,
            status="保留", taikai=False, match_state="対象外"))
        return
    comp = st.text_input("会社名", value=data.get("会社名", ""), key=f"pay_comp_{idx}")
    cc0, cc1, cc2, cc3 = st.columns(4)
    # 赤伝(マイナス請求)も扱えるよう min_value は設定しない
    cur_ex = cc0.number_input("当月税抜額（突合用）", value=int(data.get("当月税抜額", 0) or 0),
                              step=1, key=f"pay_curex_{idx}",
                              help="NE発注は税抜のため、突合はこの税抜額で行います。赤伝はマイナス可。")
    cur = cc1.number_input("当月請求額（税込・振込用）", value=int(data.get("当月請求額", 0) or 0),
                           step=1, key=f"pay_cur_{idx}")
    tot = cc2.number_input("今回請求額(繰越込)", value=int(data.get("今回請求額", 0) or 0),
                           step=1, key=f"pay_tot_{idx}")
    carry = cc3.number_input("前月繰越額", value=int(data.get("前月繰越額", 0) or 0),
                             step=1, key=f"pay_carry_{idx}")
    cd1, cd2, cd3 = st.columns(3)
    bill_date = cd1.text_input("請求日", value=data.get("請求日", ""), key=f"pay_bd_{idx}")
    due = cd2.text_input("支払期日", value=data.get("支払期日", ""), key=f"pay_due_{idx}")
    cat = cd3.selectbox("カテゴリ", ["", "WEB発行", "郵送", "前払い"], key=f"pay_cat_{idx}")
    tax_bd = st.text_input("税内訳（税率別。軽減税率対応）",
                           value=data.get("税内訳", ""), key=f"pay_tax_{idx}")
    if data.get("軽減税率"):
        st.caption("🍱 軽減税率(8%)対象品目を含む請求書です。")

    # マスタ照合
    m = look["by_norm"].get(matching.normalize_name(comp))
    # マスタ未登録 → 部分一致の候補を提示し、選べば会社名を補正
    if not m:
        try:
            cands = matching.find_candidates(comp, list(master_rows))
        except Exception:  # noqa: BLE001
            cands = []
        if cands:
            pick = st.selectbox(
                "候補から選択（部分一致。会社名の読取違いをここで補正→保存時に別名学習）",
                ["（選択しない）"] + cands, key=f"pay_cand_{idx}")
            if pick != "（選択しない）":
                comp = pick
                m = look["by_norm"].get(matching.normalize_name(comp))
                st.caption(f"→ 会社名を『{comp}』として扱います。")

    mism, note = _account_mismatch(m, data)
    if m:
        if str(m.get("支払区分", "")) == "カード払い":
            st.info(f"💳 マスタ照合: {m['会社名']}（カード払い・楽天振込CSVの対象外）")
        else:
            extra = "（複数口座中のマスタ口座に一致）" if data.get("複数口座") and not mism else ""
            st.success(f"マスタ照合: {m['会社名']}（{m.get('銀行','')} {m.get('支店','')} "
                       f"{m.get('預金種目','')} {m.get('口座番号','')}）{extra}")
    else:
        st.warning("⚠️ 会社名の不一致の可能性、または新規取引先の可能性があります。"
                   "（上の候補から選ぶか、会社名を修正してください。新規なら「取引先マスタ」で登録）")
    if mism:
        st.error(f"⚠️ 口座変更の可能性: {note}")
    elif data.get("複数口座") and not m:
        st.warning("請求書に複数の振込先口座が記載されています。どれに振込むか要確認。")
    if data.get("信頼度メモ"):
        st.caption(f"AIメモ: {data.get('信頼度メモ')}")

    # 登録ステータス: 既定は『読取済』。要確認のものだけ手動で『保留』にする運用。
    reg_status = st.selectbox(
        "登録ステータス", ["読取済", "保留"], index=0,
        key=f"pay_status_{idx}",
        help="既定は読取済。あとで確認したいものだけ『保留』にしてください。")

    rows_for_save.append(_base_row(
        data, fn, comp=comp, cur=cur, cur_ex=cur_ex, tot=tot, carry=carry,
        tax_bd=tax_bd, bill=bill_date, due=due, cat=cat, mism=mism,
        status=reg_status, taikai=True, match_state="未突合"))


# 同一フォルダのファイルは1つのアコーディオンにまとめる(判定は各ファイル個別)
from collections import OrderedDict
_groups = OrderedDict()
for idx, data in enumerate(results):
    fn = data.get("_file", f"file{idx}")
    folder = fn.rsplit("/", 1)[0] if "/" in fn else None
    gkey = ("folder", folder) if folder else ("single", idx)
    _groups.setdefault(gkey, []).append((idx, data))

rows_for_save = []
for gkey, items in _groups.items():
    grp_issue = any(_file_issue(d) for _, d in items)
    if gkey[0] == "folder":
        icon = "⚠️" if grp_issue else "✅"
        title = f"{icon} 📁 {gkey[1]}（{len(items)}ファイル）"
        multi = True
    else:
        idx, d = items[0]
        icon = "⛔" if d.get("_error") else ("⚠️" if grp_issue else "✅")
        title = f"{icon} {d.get('_file','')} — {d.get('会社名', '(会社名不明)')}"
        multi = False
    with st.expander(title, expanded=grp_issue):
        for idx, d in items:
            if multi:
                fi = "⛔" if d.get("_error") else ("⚠️" if _file_issue(d) else "✅")
                st.markdown(f"#### {fi} {d.get('_file','').rsplit('/', 1)[-1]} "
                            f"— {d.get('会社名', '(会社名不明)')}")
            _render_file(idx, d)
            if multi:
                st.divider()

st.markdown("---")
if st.button("💾 読取済として登録", type="primary", key="payable_save_btn"):
    if not target_ym.strip():
        st.error("対象月を入力してください。")
        st.stop()
    saved = 0
    learned = 0
    for r in rows_for_save:
        if not str(r["会社名"]).strip():
            continue
        try:
            N.save_invoice(db_ids, r)
            saved += 1
            # 学習: 手修正で会社名が変わり、修正後がマスタに一致するなら、
            # AIの読取値(誤読)を別名として登録 → 次回から自動補正される
            ai_name = str(r.get("_ai会社名", "")).strip()
            fixed = str(r["会社名"]).strip()
            if (ai_name and matching.normalize_name(ai_name) != matching.normalize_name(fixed)
                    and look["by_norm"].get(matching.normalize_name(fixed))):
                try:
                    if N.add_alias_by_company(db_ids, fixed, ai_name):
                        learned += 1
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            st.error(f"{r['会社名']} の登録に失敗: {e}")
    st.session_state.pop("payable_extracted", None)
    st.session_state.pop("payable_registered", None)  # 登録済み一覧を最新化
    st.session_state.pop("match_invoices", None)
    if learned:
        st.session_state["payable_master_nonce"] = st.session_state.get("payable_master_nonce", 0) + 1
    msg = f"{saved}件を「読取済」で登録しました。次は『突合確認』ページへ。"
    if learned:
        msg += f"（{learned}件の会社名の読み間違いを別名として学習しました）"
    st.success(msg)
