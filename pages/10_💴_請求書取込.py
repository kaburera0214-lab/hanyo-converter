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
st.title("💴 請求書取込（買掛）")
st.caption("請求書PDF/画像をAIで読み取り、口座マスタと照合して登録します。")

from lib.payable import app_init, extract, matching, notion_payable as N

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

# 対象月
c1, c2 = st.columns([1, 3])
target_ym = c1.text_input("対象月（例 2026-05）", value=st.session_state.get("payable_target_ym", ""),
                          key="payable_ym_in")
st.session_state["payable_target_ym"] = target_ym

st.markdown("### 1. 請求書をアップロード")
files = st.file_uploader("PDF / 画像（複数可）", type=["pdf", "png", "jpg", "jpeg", "webp"],
                         accept_multiple_files=True, key="payable_uploader")

use_sonnet = st.checkbox("読みにくい請求書（スキャン等）はSonnetで精度優先", value=False,
                         key="payable_sonnet")

if files and st.button("🤖 AIで読み取る", type="primary", key="payable_extract_btn"):
    results = []
    prog = st.progress(0.0)
    for i, f in enumerate(files):
        data = (extract.extract_invoice(f.getvalue(), f.name, model=extract.SONNET)
                if use_sonnet else extract.extract_with_fallback(f.getvalue(), f.name))
        data["_file"] = f.name
        results.append(data)
        prog.progress((i + 1) / len(files))
    st.session_state["payable_extracted"] = results
    prog.empty()

results = st.session_state.get("payable_extracted", [])
if not results:
    st.info("ファイルをアップロードして「AIで読み取る」を押してください。")
    st.stop()


def _account_mismatch(m, data):
    """マスタ口座と抽出口座の相違を判定。比較できる項目だけ見る。"""
    if not m:
        return False, ""
    notes = []
    acc_x = "".join(ch for ch in str(data.get("口座番号", "")) if ch.isdigit())
    acc_m = "".join(ch for ch in str(m.get("口座番号", "")) if ch.isdigit())
    if acc_x and acc_m and acc_x.lstrip("0") != acc_m.lstrip("0"):
        notes.append(f"口座番号 抽出{acc_x}≠マスタ{acc_m}")
    bank_x = str(data.get("振込先銀行", "")).strip()
    bank_m = str(m.get("銀行", "")).strip()
    if bank_x and bank_m and bank_m not in bank_x and bank_x not in bank_m:
        notes.append(f"銀行 抽出『{bank_x}』≠マスタ『{bank_m}』")
    return (len(notes) > 0), " / ".join(notes)


st.markdown("### 2. 読取結果の確認")
st.caption("内容を確認し、必要なら金額・会社名を修正してから登録してください。")

rows_for_save = []
for idx, data in enumerate(results):
    fn = data.get("_file", f"file{idx}")
    with st.expander(f"📄 {fn} — {data.get('会社名', '(会社名不明)')}", expanded=True):
        if data.get("_error"):
            st.error(data["_error"])
            continue
        comp = st.text_input("会社名", value=data.get("会社名", ""), key=f"pay_comp_{idx}")
        cc1, cc2, cc3 = st.columns(3)
        cur = cc1.number_input("当月請求額", value=int(data.get("当月請求額", 0) or 0),
                               step=1, key=f"pay_cur_{idx}")
        tot = cc2.number_input("今回請求額(繰越込)", value=int(data.get("今回請求額", 0) or 0),
                               step=1, key=f"pay_tot_{idx}")
        carry = cc3.number_input("前月繰越額", value=int(data.get("前月繰越額", 0) or 0),
                                 step=1, key=f"pay_carry_{idx}")
        cd1, cd2, cd3 = st.columns(3)
        bill_date = cd1.text_input("請求日", value=data.get("請求日", ""), key=f"pay_bd_{idx}")
        due = cd2.text_input("支払期日", value=data.get("支払期日", ""), key=f"pay_due_{idx}")
        cat = cd3.selectbox("カテゴリ", ["", "WEB発行", "郵送", "前払い"],
                            key=f"pay_cat_{idx}")

        # マスタ照合
        m = look["by_norm"].get(matching.normalize_name(comp))
        if m:
            st.success(f"マスタ照合: {m['会社名']}（{m.get('銀行','')} {m.get('支店番号','')} "
                       f"{m.get('預金種目','')} {m.get('口座番号','')}）")
        else:
            st.warning("⚠️ マスタ未登録の会社です。新規取引先の可能性。"
                       "「取引先マスタ」ページで登録するまで振込CSVには出ません。")
        mism, note = _account_mismatch(m, data)
        if mism:
            st.error(f"⚠️ 口座変更の可能性: {note}")
        if data.get("複数口座"):
            st.warning("請求書に複数の振込先口座が記載されています。どれに振込むか要確認。")
        if data.get("信頼度メモ"):
            st.caption(f"AIメモ: {data.get('信頼度メモ')}")

        rows_for_save.append({
            "会社名": comp, "当月請求額": cur, "今回請求額": tot, "前月繰越額": carry,
            "消費税額": data.get("消費税額", 0), "請求日": bill_date, "支払期日": due,
            "カテゴリ": cat,
            "抽出_銀行": data.get("振込先銀行", ""), "抽出_支店": data.get("振込先支店", ""),
            "抽出_預金種目": data.get("預金種目", ""), "抽出_口座番号": data.get("口座番号", ""),
            "抽出_口座名義": data.get("口座名義", ""),
            "口座相違フラグ": mism,
            "抽出メモ": data.get("信頼度メモ", ""),
            "ファイルリンク": fn,
            "ステータス": "読取済", "突合状態": "未突合",
            "対象月": target_ym,
        })

st.markdown("---")
if st.button("💾 読取済として登録", type="primary", key="payable_save_btn"):
    if not target_ym.strip():
        st.error("対象月を入力してください。")
        st.stop()
    saved = 0
    for r in rows_for_save:
        if not str(r["会社名"]).strip():
            continue
        try:
            N.save_invoice(db_ids, r)
            saved += 1
        except Exception as e:  # noqa: BLE001
            st.error(f"{r['会社名']} の登録に失敗: {e}")
    st.session_state.pop("payable_extracted", None)
    st.success(f"{saved}件を「読取済」で登録しました。次は『突合確認』ページへ。")
