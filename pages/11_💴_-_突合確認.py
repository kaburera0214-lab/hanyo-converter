# -*- coding: utf-8 -*-
"""
突合確認（買掛）

ネクストエンジンの発注データCSVをアップロードし、対象月(作成日=発注日 1〜末日)で
仕入先cd単位に合算。読取済の請求書と「会社名＋金額（許容誤差）」で突合し、
一致は緑・不一致は赤で表示。人が確認して「確認済→突合OK」に進める。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="突合確認", layout="wide")
st.title("💴 突合確認（発注データとの照合）")
st.caption("ネクストエンジン発注データと請求書を会社名＋金額で突合します。")

from lib.payable import app_init, matching, extract, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 1])
target_ym = c1.text_input("対象月（例 2026-05）", value=st.session_state.get("payable_target_ym", ""),
                          key="match_ym")
st.session_state["payable_target_ym"] = target_ym
tol = c2.number_input("許容誤差（円）", min_value=0, value=0, step=1, key="match_tol",
                      help="請求額とNE合算額の差がこの範囲内なら『一致』とみなします。")
if c3.button("🔄 請求書を再読込", key="match_reload"):
    st.session_state.pop("match_invoices", None)

try:
    y, m = (int(target_ym.split("-")[0]), int(target_ym.split("-")[1])) if "-" in target_ym else (None, None)
except (ValueError, IndexError):
    y, m = None, None

st.markdown("### 1. ネクストエンジン発注データCSV")
ne_file = st.file_uploader("発注データCSV（Shift-JIS/UTF-8）", type=["csv"], key="match_ne")
if ne_file is not None:
    st.session_state["match_ne_bytes"] = ne_file.getvalue()

ne_bytes = st.session_state.get("match_ne_bytes")
if not ne_bytes:
    st.info("発注データCSVをアップロードしてください。")
    st.stop()
if not (y and m):
    st.warning("対象月を YYYY-MM 形式で入力してください。")
    st.stop()

ne_rows = matching.read_ne_rows(ne_bytes)
ne_agg = matching.aggregate_ne(ne_rows, y, m)
st.success(f"発注データ {len(ne_rows)}行 / 対象月 {target_ym} の仕入先 {len(ne_agg)}件を合算しました。")

# 請求書(読取済以降)を取得
if "match_invoices" not in st.session_state:
    st.session_state["match_invoices"] = N.load_invoices(db_ids, target_ym=target_ym)
invoices = st.session_state["match_invoices"]
if not invoices:
    st.warning(f"対象月 {target_ym} の請求書がありません。先に『請求書取込』で登録してください。")
    st.stop()

master_rows = N.load_master(db_ids)
look = matching.build_master_lookup(master_rows)

def _extax(inv):
    """突合に使う当月税抜額。無ければ当月請求額(税込)で代替。"""
    v = inv.get("当月税抜額")
    return v if v else (inv.get("当月請求額") or 0)


st.markdown("### 2. 突合結果")
st.caption("NE発注データは税抜のため、突合は『当月税抜額』で行います（振込CSVは税込で作成）。")
if st.button("🔁 突合を実行/再計算", type="primary", key="match_run"):
    for inv in invoices:
        if inv.get("突合状態") == "対象外":
            continue  # 「突合しない」指定のファイルはスキップ(保持はする)
        r = matching.match_invoice(inv["会社名"], _extax(inv), look, ne_agg, tolerance=tol)
        denpyo = ",".join(str(d) for d in r.get("NE伝票", []))
        try:
            N.update_invoice_fields(
                db_ids, inv["id"],
                突合状態=r["状態"],
                NE合算額=r["NE合算額"], NE送料=r.get("NE送料", 0),
                差額=r["差額"], NE発注番号=denpyo,
            )
            inv["突合状態"] = r["状態"]
            inv["NE合算額"] = r["NE合算額"]
            inv["NE送料"] = r.get("NE送料", 0)
            inv["NE合計"] = r.get("NE合計")
            inv["差額"] = r["差額"]
            inv["NE発注番号"] = denpyo
        except Exception as e:  # noqa: BLE001
            st.error(f"{inv['会社名']} の更新に失敗: {e}")
    st.toast("突合を更新しました")


# 数値整形: 小数があれば表示、なければ整数(カンマ区切り)
def yen(v):
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"


def _row_color(stt):
    return {
        "一致": "background-color:#e6f4ea",
        "金額不一致": "background-color:#fde8e8",
        "発注なし": "background-color:#fff4e5",
        "マスタ未登録": "background-color:#fffbe6",
    }.get(stt, "")


def _ne_total(i):
    if i.get("NE合計") is not None:
        return i.get("NE合計")
    if i.get("NE合算額") is not None:
        return (i.get("NE合算額") or 0) + (i.get("NE送料") or 0)
    return None


def _kamoku(inv):
    mm = look["by_norm"].get(matching.normalize_name(inv["会社名"]))
    return str(mm.get("科目", "")) if mm else ""


def _shiire_no_order(inv):
    """科目が仕入なのに発注なし(=締め跨ぎ等の要調査)。"""
    return inv.get("突合状態") == "発注なし" and _kamoku(inv) == "仕入"

df = pd.DataFrame([{
    "会社名": i["会社名"], "当月税抜(突合)": yen(_extax(i)),
    "NE発注額": yen(i.get("NE合算額")), "送料": yen(i.get("NE送料")),
    "NE合計": yen(_ne_total(i)), "差額": yen(i.get("差額")),
    "突合状態": (i.get("突合状態", "未突合")
              + ("（仕入・締め跨ぎ?）" if _shiire_no_order(i) else "")),
    "当月税込": yen(i["当月請求額"]),
    "ステータス": i["ステータス"], "口座相違": "⚠️" if i.get("口座相違フラグ") else "",
} for i in invoices])

# 行の色: 金額不一致、または『仕入なのに発注なし』は赤
_colors = []
for i in invoices:
    stt = i.get("突合状態", "未突合")
    if stt == "金額不一致" or _shiire_no_order(i):
        _colors.append("background-color:#fde8e8")
    else:
        _colors.append(_row_color(stt))
styled = df.style.apply(lambda s: _colors, subset=["突合状態"])
st.dataframe(styled, use_container_width=True)

n_ok = sum(1 for i in invoices if i.get("突合状態") == "一致")
n_err = sum(1 for i in invoices if i.get("突合状態") in ("金額不一致", "発注なし"))
st.caption(f"一致 {n_ok}件 / 要確認 {n_err}件 / 全{len(invoices)}件")
if any(_shiire_no_order(i) for i in invoices):
    st.warning("🔴 科目『仕入』なのに発注が見つからない取引先があります。"
               "締め日の跨ぎ（月初/末日でのズレ）の可能性があるため、NEの発注日や前後月をご確認ください。")

# 同一会社名の重複検知(突合対象外=対象外は除外)
from collections import defaultdict
_groups = defaultdict(list)
for inv in invoices:
    if inv.get("突合状態") == "対象外":
        continue
    _groups[matching.normalize_name(inv["会社名"])].append(inv)
_dups = {k: v for k, v in _groups.items() if len(v) > 1}
if _dups:
    st.markdown("### ⚠️ 同一会社名の重複")
    st.caption(f"{len(_dups)}社で会社名が重複しています。残すレコードを選び、それ以外を削除できます。")
    for norm, recs in _dups.items():
        with st.container(border=True):
            st.markdown(f"**{recs[0]['会社名']}**（{len(recs)}件）")
            labels = [f"{yen(r['当月請求額'])}円 / {r.get('ファイルリンク','') or '(ファイル名なし)'} "
                      f"/ {r['ステータス']} / {r.get('突合状態','')}" for r in recs]
            keep = st.radio("残すレコード", options=list(range(len(recs))),
                            format_func=lambda j, _l=labels: _l[j], key=f"dupkeep_{norm}")
            with st.popover("🗑️ 選択以外を削除"):
                st.warning("⚠️ 必ず請求書（PDF）の内容を確認してから実行してください。"
                           "削除は取り消せません。")
                ok = st.checkbox("請求書を確認しました", key=f"dupok_{norm}")
                if st.button("選択以外を削除する", type="primary", disabled=not ok,
                             key=f"dupbtn_{norm}"):
                    for j, r in enumerate(recs):
                        if j != keep:
                            N.delete_invoice(db_ids, r["id"])
                    st.session_state.pop("match_invoices", None)
                    st.rerun()

st.markdown("### 3. ステータス更新")
st.caption("各請求書を確認し、問題なければ『確認済』にしてください"
           "（確認済＝振込CSVに出してよい最終承認）。保留・対象外はここには出ません。")

# プレビュー用に請求書ファイルを読み込む(任意)。ファイル名一致で各行に表示。
with st.expander("📄 プレビュー用に請求書ファイルを読み込む（任意・ZIP可）", expanded=False):
    prev_files = st.file_uploader("PDF / 画像 / ZIP", type=["pdf", "png", "jpg", "jpeg", "webp", "zip"],
                                  accept_multiple_files=True, key="match_prevup")
    if prev_files:
        st.session_state["match_filebytes"] = {
            n: b for n, b in extract.iter_files_from_uploads(prev_files)}
        st.caption(f"{len(st.session_state['match_filebytes'])}件を読み込みました。")
fbmap = st.session_state.get("match_filebytes", {})

NE_URL = "https://main.next-engine.com/userg5210?dnum={}"
_ICON = {"一致": "✅", "金額不一致": "⚠️", "発注なし": "🟠", "マスタ未登録": "🟡", "未突合": "⬜"}
_STAT = ["読取済", "確認済"]  # 簡素化: 読取済→確認済の2段(確認済=振込CSV対象)

# 対象外・保留はこのページには出さない(取込ページで扱う)
visible = [i for i in invoices
           if i.get("突合状態") != "対象外" and i.get("ステータス") != "保留"]
if not visible:
    st.info("表示対象の請求書がありません（保留・対象外を除く）。")
for inv in visible:
    stt = inv.get("突合状態", "未突合")
    icon = "🔴" if _shiire_no_order(inv) else _ICON.get(stt, "")
    diff = inv.get("差額")
    head = (f"{icon} {inv['会社名']}　|　突合 {stt}"
            f"（差額 {yen(diff)}円）　|　ステータス: {inv['ステータス']}")
    with st.expander(head, expanded=(stt != "一致")):
        if _shiire_no_order(inv):
            st.error("🔴 科目『仕入』なのに発注なし。締め日の跨ぎ（月初/末日のズレ）の可能性。"
                     "NEの発注日・前後月をご確認ください。")
        if fbmap.get(inv.get("ファイルリンク", "")):
            if st.toggle("📄 プレビュー表示", key=f"match_prev_{inv['id']}"):
                for _img in extract.render_preview_images(
                        fbmap[inv["ファイルリンク"]], inv["ファイルリンク"]):
                    st.image(_img, use_container_width=True)
        cL, cR = st.columns(2)
        # 左: 金額・突合
        cL.markdown(
            f"**金額**\n\n"
            f"- 当月税抜(突合用): **{yen(_extax(inv))}** 円\n"
            f"- 当月請求(税込・振込用): **{yen(inv.get('当月請求額'))}** 円\n"
            f"- 今回請求(繰越込): {yen(inv.get('今回請求額'))} 円 / 前月繰越: {yen(inv.get('前月繰越額'))} 円\n"
            f"- 税内訳: {inv.get('税内訳','') or '—'}"
            f"{'　🍱軽減税率' if inv.get('軽減税率') else ''}\n\n"
            f"**突合**\n\n"
            f"- NE発注額(税抜): {yen(inv.get('NE合算額'))} ＋ 送料: {yen(inv.get('NE送料'))} "
            f"= NE合計: **{yen(_ne_total(inv))}** 円\n"
            f"- 差額: **{yen(diff)}** 円")
        denpyo = [d for d in str(inv.get("NE発注番号", "")).split(",") if d.strip()]
        if denpyo:
            links = "　".join(
                f'<a href="{NE_URL.format(d.strip())}" target="_blank" rel="noopener">📄{d.strip()}</a>'
                for d in denpyo)
            cL.markdown(f"NE発注書: {links}", unsafe_allow_html=True)
        # 右: 口座・取引先・期日・ファイル
        cR.markdown(
            f"**振込先（請求書から抽出）**\n\n"
            f"- {inv.get('抽出_銀行','') or '—'} {inv.get('抽出_支店','')} "
            f"{inv.get('抽出_預金種目','')} {inv.get('抽出_口座番号','')}\n"
            f"- 名義: {inv.get('抽出_口座名義','') or '—'}\n"
            f"{'- ⚠️ **口座変更の可能性あり**' if inv.get('口座相違フラグ') else ''}\n\n"
            f"**その他**\n\n"
            f"- 請求日: {inv.get('請求日','') or '—'} / 支払期日: {inv.get('支払期日','') or '—'}\n"
            f"- カテゴリ: {inv.get('カテゴリ','') or '—'}\n"
            f"- ファイル: {inv.get('ファイルリンク','') or '—'}\n"
            f"- AIメモ: {inv.get('抽出メモ','') or '—'}")
        # 操作
        oc1, oc2, oc3 = st.columns([2, 1, 1])
        new_status = oc1.selectbox(
            "ステータス", _STAT,
            index=_STAT.index(inv["ステータス"]) if inv["ステータス"] in _STAT else 0,
            key=f"match_st_{inv['id']}")
        if oc2.button("更新", key=f"match_upd_{inv['id']}", use_container_width=True):
            N.update_invoice_fields(db_ids, inv["id"], ステータス=new_status)
            inv["ステータス"] = new_status
            st.toast(f"{inv['会社名']} → {new_status}")
            st.rerun()
        with oc3.popover("🗑️削除", use_container_width=True):
            st.warning("⚠️ 削除前に必ず請求書（PDF）の内容を確認してください。削除は取り消せません。")
            ok = st.checkbox("請求書を確認しました", key=f"delok_{inv['id']}")
            if st.button("この請求書を削除する", type="primary", disabled=not ok,
                         key=f"match_del_{inv['id']}"):
                N.delete_invoice(db_ids, inv["id"])
                st.session_state.pop("match_invoices", None)
                st.rerun()
