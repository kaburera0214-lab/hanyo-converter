# -*- coding: utf-8 -*-
"""
口座振替チェック（買掛）

口座振替（引落）の書類（請求書・利用明細・検針票など）をAI読取で登録し、
口座振替ならではのチェックを行う専用ページ。突合（NE照合）や振込CSVには乗らない。

チェック内容:
  - 固定額の取引先: マスタの固定額と一致するか
  - 変動はあるが固定的な取引先(水道光熱費・コピー代・配送料等): 過去平均±許容%以内か
  - 未登録: 引落があるはずの取引先で当月の登録が無いものを一覧化
  - 手入力: 書類が無い引落(通帳ベース等)は金額を直接登録できる

登録レコードは 支払_請求書 に「突合状態=口座振替」で保存し、
支払ダッシュボード(銀行別資金必要額・カレンダー等)に集計される。
"""
import re
import unicodedata

import streamlit as st
import pandas as pd

st.set_page_config(page_title="口座振替チェック", layout="wide")

from lib.auth import require_role
require_role("payable")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("💴 口座振替チェック")
st.caption("口座振替（引落）の書類をAI読取で登録し、固定額・変動幅・未登録をチェックします。")

from lib.payable import app_init, extract, matching, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

if st.button("🔄 最新に更新", key="kz_reload"):
    for k in ("kz_invoices", "kz_master"):
        st.session_state.pop(k, None)
if "kz_invoices" not in st.session_state:
    with st.spinner("読込中…"):
        st.session_state["kz_invoices"] = N.load_invoices(db_ids)
        st.session_state["kz_master"] = N.load_master(db_ids)
invoices_all = st.session_state["kz_invoices"]
master_rows = st.session_state["kz_master"]
look = matching.build_master_lookup(master_rows)

# 口座振替の取引先（マスタの支払方法ベース）
kz_masters = [m for m in master_rows if "口座振替" in str(m.get("支払方法", ""))]


def yen(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v or "")
    return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"


def pay_day_key(s):
    s = unicodedata.normalize("NFKC", str(s or ""))
    if "末" in s:
        return 31
    m = re.search(r"(\d{1,2})\s*日", s)
    if m:
        return int(m.group(1))
    m = re.search(r"/(\d{1,2})", s)
    if m:
        return int(m.group(1))
    return 99


c1, c2 = st.columns([1, 1])
target_ym = c1.text_input("対象月（例 2026-05）",
                          value=st.session_state.get("payable_target_ym", ""), key="kz_ym")
st.session_state["payable_target_ym"] = target_ym
var_th = c2.number_input("変動の許容幅（±％）", 5, 100, 20, step=5, key="kz_varth",
                         help="過去6ヶ月平均からこの割合以上ズレたら要確認にします。")

# 会社×月の金額集計（正式名称へ名寄せ。対象外のみ除外、口座振替は含む）
sums = {}
for inv in invoices_all:
    if inv.get("突合状態") == "対象外":
        continue
    ym = str(inv.get("対象月", "")).strip()
    if not re.match(r"^\d{4}-\d{2}$", ym):
        continue
    m = look["by_norm"].get(matching.normalize_name(inv["会社名"]))
    name = m["会社名"] if m else inv["会社名"]
    sums[(name, ym)] = sums.get((name, ym), 0) + float(inv.get("当月請求額") or 0)
all_months = sorted({ym for (_, ym) in sums})


def month_amount(name, ym):
    return sums.get((name, ym), 0)


def hist_avg(name, before_ym, n=6):
    """before_ymより前の直近nヶ月のうち、支払があった月の平均。"""
    hist = [m for m in all_months if m < before_ym][-n:]
    vals = [month_amount(name, m) for m in hist]
    vals = [v for v in vals if v > 0]
    return (sum(vals) / len(vals)) if vals else None


# ============================================================
# 1. 書類の取込（請求書取込と同じAI読取。登録先は突合状態=口座振替）
# ============================================================
st.markdown("### 1. 引落書類の取込（PDF/画像/ZIP）")
up_key = st.session_state.get("kz_up_key", 0)
files = st.file_uploader("請求書・利用明細・検針票など（複数可）",
                         type=["pdf", "png", "jpg", "jpeg", "webp", "zip"],
                         accept_multiple_files=True, key=f"kz_uploader_{up_key}")
entries = extract.iter_files_from_uploads(files)
uc1, uc2 = st.columns([1, 3])
if files:
    uc2.caption(f"読取対象：{len(entries)}件")
if uc1.button("🗑️ 読込結果をクリア", key="kz_clear"):
    st.session_state.pop("kz_extracted", None)
    st.session_state["kz_up_key"] = up_key + 1
    st.rerun()

use_sonnet = st.checkbox("読みにくい書類（スキャン等）はSonnetで精度優先", value=False,
                         key="kz_sonnet")
if entries and st.button("🤖 AIで読み取る", type="primary", key="kz_extract"):
    results = []
    prog = st.progress(0.0)
    for i, (fname, fbytes) in enumerate(entries):
        d = (extract.extract_invoice(fbytes, fname, model=extract.SONNET)
             if use_sonnet else extract.extract_with_fallback(fbytes, fname))
        d["_file"] = fname
        results.append(d)
        prog.progress((i + 1) / len(entries))
    st.session_state["kz_extracted"] = results
    prog.empty()

_save_msg = st.session_state.pop("kz_save_msg", None)
if _save_msg:
    st.success(_save_msg)

results = st.session_state.get("kz_extracted", [])
rows_for_save = []
if results:
    st.markdown("#### 読取結果の確認")
    for idx, d in enumerate(results):
        fn = d.get("_file", f"file{idx}")
        if d.get("_error"):
            st.error(f"{fn}: {d['_error']}")
            continue
        m0 = look["by_norm"].get(matching.normalize_name(d.get("会社名", "")))
        is_kz = m0 is not None and "口座振替" in str(m0.get("支払方法", ""))
        icon = "✅" if is_kz else "⚠️"
        with st.expander(f"{icon} {fn} — {d.get('会社名','(不明)')}", expanded=not is_kz):
            comp = st.text_input("会社名", value=d.get("会社名", ""), key=f"kz_comp_{idx}")
            m = look["by_norm"].get(matching.normalize_name(comp))
            if not m:
                try:
                    cands = matching.find_candidates(comp, list(master_rows))
                except Exception:  # noqa: BLE001
                    cands = []
                if cands:
                    pick = st.selectbox("候補から選択", ["（選択しない）"] + cands,
                                        key=f"kz_cand_{idx}")
                    if pick != "（選択しない）":
                        comp = pick
                        m = look["by_norm"].get(matching.normalize_name(comp))
            if m and "口座振替" not in str(m.get("支払方法", "")):
                st.warning(f"この取引先の支払方法は『{m.get('支払方法','')}』です。"
                           "口座振替でなければ『請求書取込』ページから登録してください。")
            elif not m:
                st.warning("会社名の不一致の可能性、または新規取引先の可能性があります。")
            else:
                st.success(f"マスタ照合: {m['会社名']}（{m.get('科目','')} / "
                           f"引落日 {m.get('支払日','') or '—'} / {m.get('支払元銀行','')}）")
            k1, k2 = st.columns(2)
            amt = k1.number_input("当月請求額（税込）", value=int(d.get("当月請求額", 0) or 0),
                                  step=1, key=f"kz_amt_{idx}")
            bd = k2.text_input("請求日", value=d.get("請求日", ""), key=f"kz_bd_{idx}")
            if d.get("信頼度メモ"):
                st.caption(f"AIメモ: {d.get('信頼度メモ')}")
            rows_for_save.append({
                "会社名": comp, "当月請求額": amt, "当月税抜額": d.get("当月税抜額", 0),
                "今回請求額": d.get("今回請求額", 0), "前月繰越額": d.get("前月繰越額", 0),
                "消費税額": d.get("消費税額", 0), "税内訳": d.get("税内訳", ""),
                "軽減税率": d.get("軽減税率", False),
                "請求日": bd, "支払期日": "", "カテゴリ": "",
                "抽出メモ": d.get("信頼度メモ", ""), "ファイルリンク": fn,
                "ステータス": "読取済", "突合状態": "口座振替", "突合対象": False,
                "対象月": target_ym,
            })

    if rows_for_save and st.button("💾 口座振替として登録", type="primary", key="kz_save"):
        if not target_ym.strip():
            st.error("対象月を入力してください。")
            st.stop()
        try:
            existing = {e.get("ファイルリンク", ""): e
                        for e in N.load_invoices(db_ids, target_ym=target_ym)
                        if e.get("ファイルリンク", "")}
        except Exception:  # noqa: BLE001
            existing = {}
        saved = replaced = 0
        for r in rows_for_save:
            if not str(r["会社名"]).strip():
                continue
            old = existing.get(r.get("ファイルリンク", ""))
            try:
                if old and old["ステータス"] in ("保留", "読取済"):
                    N.delete_invoice(db_ids, old["id"])
                    N.save_invoice(db_ids, r)
                    replaced += 1
                elif not old:
                    N.save_invoice(db_ids, r)
                    saved += 1
            except Exception as e:  # noqa: BLE001
                st.error(f"{r['会社名']} の登録に失敗: {e}")
        st.session_state.pop("kz_extracted", None)
        st.session_state.pop("kz_invoices", None)
        st.session_state["kz_save_msg"] = (
            f"新規{saved}件を口座振替として登録しました"
            + (f"（再登録{replaced}件は上書き）" if replaced else "") + "。")
        st.rerun()

# ============================================================
# 2. 当月チェックリスト（固定額・変動幅・未登録）
# ============================================================
st.markdown(f"### 2. 当月チェックリスト（{target_ym or '対象月未入力'}）")
if not target_ym.strip():
    st.info("対象月を入力するとチェックリストを表示します。")
    st.stop()
if not kz_masters:
    st.info("マスタに支払方法が『口座振替』の取引先がありません。")
    st.stop()

check_rows = []
n_ok = n_warn = n_miss = 0
for m in sorted(kz_masters, key=lambda x: pay_day_key(x.get("支払日", ""))):
    name = m["会社名"]
    cur = month_amount(name, target_ym)
    avg = hist_avg(name, target_ym)
    fixed = m.get("固定額", "")
    try:
        fixed_v = float(str(fixed).replace(",", "")) if str(fixed).strip() else None
    except ValueError:
        fixed_v = None
    if cur <= 0:
        judge, detail = "📭 未登録", "書類の取込 or 手入力で登録してください"
        n_miss += 1
    elif fixed_v is not None:
        if abs(cur - fixed_v) < 1:
            judge, detail = "✅ 固定額どおり", ""
            n_ok += 1
        else:
            judge = "⚠️ 固定額とズレ"
            detail = f"固定額 {yen(fixed_v)}円 との差 {yen(cur - fixed_v)}円 → 原因を確認"
            n_warn += 1
    elif avg:
        rate = (cur - avg) / avg * 100
        if abs(rate) <= var_th:
            judge, detail = "✅ 変動幅内", f"過去平均 {yen(avg)}円（{rate:+.1f}%）"
            n_ok += 1
        else:
            judge = "⚠️ 変動大"
            detail = f"過去平均 {yen(avg)}円 から {rate:+.1f}% → 使用量増・料金改定等を確認"
            n_warn += 1
    else:
        judge, detail = "－ 履歴なし", "初回登録（次月から変動チェックが効きます）"
        n_ok += 1
    check_rows.append({
        "引落日": m.get("支払日", "") or "—", "会社名": name,
        "科目": m.get("科目", ""), "引落口座": m.get("支払元銀行", "") or "(未設定)",
        "当月額": yen(cur) if cur else "", "判定": judge, "詳細": detail,
    })

mc1, mc2, mc3 = st.columns(3)
mc1.metric("✅ OK", f"{n_ok}件")
mc2.metric("⚠️ 要確認", f"{n_warn}件")
mc3.metric("📭 未登録", f"{n_miss}件")

cdf = pd.DataFrame(check_rows)


def _judge_color(v):
    if str(v).startswith("⚠️"):
        return "background-color:#fde8e8"
    if str(v).startswith("📭"):
        return "background-color:#fff4e5"
    if str(v).startswith("✅"):
        return "background-color:#e6f4ea"
    return ""


st.dataframe(cdf.style.apply(lambda col: [_judge_color(v) for v in col], subset=["判定"]),
             use_container_width=True, hide_index=True, height=520)
total_kz = sum(month_amount(m["会社名"], target_ym) for m in kz_masters)
st.caption(f"口座振替 当月合計: {int(total_kz):,}円")

# ============================================================
# 3. 手入力での登録（書類が無い引落: 通帳・明細ベース）
# ============================================================
with st.expander("✏️ 金額を手入力で登録（書類が無い引落）", expanded=False):
    h1, h2, h3 = st.columns([2, 1, 1])
    kz_names = [m["会社名"] for m in sorted(kz_masters,
                                          key=lambda x: pay_day_key(x.get("支払日", "")))]
    sel_name = h1.selectbox("取引先", kz_names, key="kz_manual_name")
    manual_amt = h2.number_input("金額（税込）", step=1, value=0, key="kz_manual_amt")
    if h3.button("登録", key="kz_manual_save", disabled=(manual_amt == 0)):
        try:
            N.save_invoice(db_ids, {
                "会社名": sel_name, "当月請求額": manual_amt,
                "対象月": target_ym, "ファイルリンク": f"手入力_{target_ym}_{sel_name}",
                "ステータス": "読取済", "突合状態": "口座振替", "突合対象": False,
                "抽出メモ": "手入力（書類なし）",
            })
            st.session_state.pop("kz_invoices", None)
            st.success(f"{sel_name}: {manual_amt:,}円 を登録しました。")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"登録に失敗: {e}")
