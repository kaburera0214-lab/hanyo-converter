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

from lib.payable import app_init, matching, notion_payable as N

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

st.markdown("### 2. 突合結果")
if st.button("🔁 突合を実行/再計算", type="primary", key="match_run"):
    for inv in invoices:
        r = matching.match_invoice(inv["会社名"], inv["当月請求額"], look, ne_agg, tolerance=tol)
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

df = pd.DataFrame([{
    "会社名": i["会社名"], "当月請求額": yen(i["当月請求額"]),
    "NE発注額": yen(i.get("NE合算額")), "送料": yen(i.get("NE送料")),
    "NE合計": yen(_ne_total(i)), "差額": yen(i.get("差額")),
    "突合状態": i.get("突合状態", "未突合"),
    "ステータス": i["ステータス"], "口座相違": "⚠️" if i.get("口座相違フラグ") else "",
} for i in invoices])

styled = df.style.apply(lambda s: [_row_color(v) for v in df["突合状態"]], subset=["突合状態"])
st.dataframe(styled, use_container_width=True)

n_ok = sum(1 for i in invoices if i.get("突合状態") == "一致")
n_err = sum(1 for i in invoices if i.get("突合状態") in ("金額不一致", "発注なし"))
st.caption(f"一致 {n_ok}件 / 要確認 {n_err}件 / 全{len(invoices)}件")

# 同一会社名の重複検知 → どちらを残すか選択(請求書確認の確認ポップアップ付き)
from collections import defaultdict
_groups = defaultdict(list)
for inv in invoices:
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
st.caption("内容を確認したら、各請求書のステータスを進めてください（確定は『振込CSV生成』前の最終承認）。")
NE_URL = "https://main.next-engine.com/userg5210?dnum={}"
for inv in invoices:
    cols = st.columns([3, 2, 2, 2])
    cols[0].markdown(f"**{inv['会社名']}**　請求{int(inv['当月請求額']):,}円　"
                     f"({inv.get('突合状態','未突合')})")
    denpyo = [d for d in str(inv.get("NE発注番号", "")).split(",") if d.strip()]
    if denpyo:
        links = "　".join(
            f'<a href="{NE_URL.format(d.strip())}" target="_blank" rel="noopener">📄{d.strip()}</a>'
            for d in denpyo)
        cols[0].markdown(f"<span style='font-size:0.85em'>NE発注: {links}</span>",
                         unsafe_allow_html=True)
    new_status = cols[1].selectbox(
        "ステータス", ["読取済", "確認済", "突合OK", "確定"],
        index=["読取済", "確認済", "突合OK", "確定"].index(inv["ステータス"])
        if inv["ステータス"] in ["読取済", "確認済", "突合OK", "確定"] else 0,
        key=f"match_st_{inv['id']}", label_visibility="collapsed")
    if cols[2].button("更新", key=f"match_upd_{inv['id']}"):
        N.update_invoice_fields(db_ids, inv["id"], ステータス=new_status)
        inv["ステータス"] = new_status
        st.toast(f"{inv['会社名']} → {new_status}")
    with cols[3].popover("🗑️削除"):
        st.warning("⚠️ 削除前に必ず請求書（PDF）の内容を確認してください。削除は取り消せません。")
        ok = st.checkbox("請求書を確認しました", key=f"delok_{inv['id']}")
        if st.button("この請求書を削除する", type="primary", disabled=not ok,
                     key=f"match_del_{inv['id']}"):
            N.delete_invoice(db_ids, inv["id"])
            st.session_state.pop("match_invoices", None)
            st.rerun()
