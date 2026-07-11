# -*- coding: utf-8 -*-
"""
支払ダッシュボード（買掛）

毎月の支払（振込・口座振替・カード）を見える化する。
①増減アラート（過去平均比で率＋額の両方超え）・未着・新規・固定額ズレ
②取引先ルール発火（例: ヤマト『100万超で振込』）
③支払マトリクス（取引先×月）
④銀行別資金必要額（支払元銀行×支払日、口座振替込み → 資金移動の判断）
⑤資金繰りカレンダー（支払日順）
⑥科目別月次推移

データ源: 支払_請求書（取込済の全支払。口座振替分も請求書取込でAI読取して登録する運用）
        ＋ 支払_取引先マスタ（科目・支払方法・支払日・支払元銀行・固定額・ルール）
"""
import re
import unicodedata

import streamlit as st
import pandas as pd

st.set_page_config(page_title="支払ダッシュボード", layout="wide")
st.title("💴 支払ダッシュボード")
st.caption("毎月の支払を見える化：増減アラート・銀行別資金必要額・資金繰りカレンダー・科目別推移")

from lib.payable import app_init, matching, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

if st.button("🔄 最新に更新", key="dash_reload"):
    st.session_state.pop("dash_invoices", None)
    st.session_state.pop("dash_master", None)

if "dash_invoices" not in st.session_state:
    with st.spinner("支払データを読込中…"):
        st.session_state["dash_invoices"] = N.load_invoices(db_ids)
        st.session_state["dash_master"] = N.load_master(db_ids)
invoices_all = st.session_state["dash_invoices"]
master_rows = st.session_state["dash_master"]
look = matching.build_master_lookup(master_rows)


# ---------- ヘルパ ----------
def yen(v):
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"


def pay_day_key(s):
    """支払日文字列を月内ソート用の日(1-31)へ。末日=31、不明=99。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    if "末" in s:
        return 31
    m = re.search(r"(\d{1,2})\s*日", s)
    if m:
        return int(m.group(1))
    m = re.search(r"/(\d{1,2})", s)  # 8/25前後 等
    if m:
        return int(m.group(1))
    return 99


def rule_threshold(text):
    """『100万超』『1,000,000円超』のような閾値をルール/備考から抽出。無ければNone。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*万\s*超", s)
    if m:
        return int(float(m.group(1).replace(",", "")) * 10000)
    m = re.search(r"([\d,]{4,})\s*円?\s*超", s)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _canon(inv):
    """請求書の会社名をマスタの正式名称へ寄せる。"""
    m = look["by_norm"].get(matching.normalize_name(inv["会社名"]))
    return (m["会社名"] if m else inv["会社名"]), m


# ---------- データ整形 ----------
rows = []
for inv in invoices_all:
    if inv.get("突合状態") == "対象外":
        continue
    ym = str(inv.get("対象月", "")).strip()
    if not re.match(r"^\d{4}-\d{2}$", ym):
        continue
    name, m = _canon(inv)
    rows.append({
        "会社名": name, "対象月": ym,
        "金額": float(inv.get("当月請求額") or 0),
        "科目": (m or {}).get("科目", "") or "(未設定)",
        "支払方法": (m or {}).get("支払方法", "") or "(未設定)",
        "支払日": (m or {}).get("支払日", ""),
        "支払元銀行": (m or {}).get("支払元銀行", "") or "(未設定)",
        "固定額": (m or {}).get("固定額", ""),
        "ルール": ((m or {}).get("ルール", "") + " " + (m or {}).get("備考", "")).strip(),
        "ステータス": inv.get("ステータス", ""),
    })

if not rows:
    st.info("支払データがまだありません。『請求書取込』で登録すると集計されます"
            "（口座振替分も同じ取込フローで読み取れます）。")
    st.stop()

df = pd.DataFrame(rows)
months = sorted(df["対象月"].unique())

# ---------- フィルタ・しきい値 ----------
f1, f2, f3, f4 = st.columns([1, 1, 1, 1])
target = f1.selectbox("対象月", list(reversed(months)), index=0, key="dash_target")
n_show = f2.slider("表示月数", 3, 24, 12, key="dash_nshow")
rate_th = f3.number_input("増減アラート閾値（％）", 5, 200, 30, step=5, key="dash_rate")
amt_th = f4.number_input("増減アラート閾値（円）", 0, 10_000_000, 50_000, step=10_000,
                         key="dash_amt")

show_months = [m for m in months if m <= target][-n_show:]
pivot = (df.pivot_table(index="会社名", columns="対象月", values="金額", aggfunc="sum")
         .fillna(0.0))
for m in show_months:
    if m not in pivot.columns:
        pivot[m] = 0.0

info_cols = (df.sort_values("対象月")
             .groupby("会社名")[["科目", "支払方法", "支払日", "支払元銀行", "固定額", "ルール"]]
             .last())

# 過去平均（対象月より前の直近6ヶ月、支払があった月のみ）
hist_months = [m for m in months if m < target][-6:]


def _hist_avg(name):
    if not hist_months:
        return None
    vals = [pivot.at[name, m] for m in hist_months if m in pivot.columns and pivot.at[name, m] > 0]
    return (sum(vals) / len(vals)) if vals else None


# ---------- ① アラート ----------
st.markdown("## 🚨 アラート")
alerts_updown, alerts_miss, alerts_new, alerts_rule, alerts_fixed = [], [], [], [], []
for name in pivot.index:
    cur = pivot.at[name, target] if target in pivot.columns else 0.0
    avg = _hist_avg(name)
    info = info_cols.loc[name] if name in info_cols.index else {}
    # 増減（率＋額の両方）
    if avg and cur > 0:
        diff = cur - avg
        rate = diff / avg * 100
        if abs(rate) >= rate_th and abs(diff) >= amt_th:
            alerts_updown.append((name, cur, avg, diff, rate))
    # 未着（直近3ヶ月中2ヶ月以上支払があるのに今月ゼロ）
    recent3 = [m for m in months if m < target][-3:]
    paid_recent = sum(1 for m in recent3 if m in pivot.columns and pivot.at[name, m] > 0)
    if cur == 0 and paid_recent >= 2:
        alerts_miss.append((name, paid_recent))
    # 新規（過去に支払なし・今月あり）
    past = [m for m in months if m < target]
    if cur > 0 and past and all(
            pivot.at[name, m] == 0 for m in past if m in pivot.columns):
        alerts_new.append((name, cur))
    # ルール発火（『N万超』等）
    th = rule_threshold(dict(info).get("ルール", "") if len(info) else "")
    if th and cur > th:
        alerts_rule.append((name, cur, th, dict(info).get("ルール", "")))
    # 固定額ズレ
    fixed = dict(info).get("固定額", "") if len(info) else ""
    try:
        fixed_v = float(str(fixed).replace(",", "")) if str(fixed).strip() else None
    except ValueError:
        fixed_v = None
    if fixed_v and cur > 0 and abs(cur - fixed_v) >= 1:
        alerts_fixed.append((name, cur, fixed_v))

# 処理漏れ
pending = [i for i in invoices_all
           if str(i.get("対象月", "")) == target and i.get("突合状態") != "対象外"
           and i.get("ステータス") in ("保留", "読取済")]

a1, a2, a3, a4, a5, a6 = st.columns(6)
a1.metric("増減あり", f"{len(alerts_updown)}件")
a2.metric("未着の可能性", f"{len(alerts_miss)}件")
a3.metric("新規取引先", f"{len(alerts_new)}件")
a4.metric("ルール該当", f"{len(alerts_rule)}件")
a5.metric("固定額ズレ", f"{len(alerts_fixed)}件")
a6.metric("未処理(保留/読取済)", f"{len(pending)}件")

if alerts_rule:
    for name, cur, th, rule in alerts_rule:
        st.error(f"📌 **{name}**: {yen(cur)}円 — ルール『{rule[:50]}』に該当"
                 f"（閾値 {yen(th)}円 超え）。支払方法の切替等をご確認ください。")
if alerts_updown:
    with st.expander(f"📈 増減アラート（平均比±{rate_th}%かつ±{yen(amt_th)}円以上）: "
                     f"{len(alerts_updown)}件", expanded=True):
        st.dataframe(pd.DataFrame([{
            "会社名": n, "今月": yen(c), "過去平均": yen(a),
            "差額": ("+" if d >= 0 else "") + yen(d),
            "増減率": f"{r:+.1f}%", "傾向": "🔺増" if d > 0 else "🔻減",
        } for n, c, a, d, r in sorted(alerts_updown, key=lambda x: -abs(x[3]))]),
            use_container_width=True)
if alerts_miss:
    with st.expander(f"📭 未着の可能性（直近は支払あり・今月ゼロ）: {len(alerts_miss)}件"):
        for n, k in alerts_miss:
            st.write(f"- {n}（直近3ヶ月中{k}ヶ月支払あり）→ 請求書の未着・取込漏れを確認")
if alerts_new:
    with st.expander(f"🆕 新規取引先: {len(alerts_new)}件"):
        for n, c in alerts_new:
            st.write(f"- {n}: {yen(c)}円")
if alerts_fixed:
    with st.expander(f"📐 固定額とのズレ: {len(alerts_fixed)}件"):
        for n, c, fv in alerts_fixed:
            st.write(f"- {n}: 今月{yen(c)}円 ≠ 固定額{yen(fv)}円 → 原因を調査"
                     "（業務委託の一律料金チェック）")
if pending:
    with st.expander(f"⏳ 未処理（保留/読取済のまま）: {len(pending)}件"):
        for i in pending:
            st.write(f"- {i['会社名']}（{i['ステータス']} / {i.get('突合状態','')}）")

# ---------- ② 支払マトリクス ----------
st.markdown("## 📊 支払マトリクス（取引先×月）")
mf1, mf2 = st.columns([1, 1])
kamoku_f = mf1.multiselect("科目で絞り込み", sorted(df["科目"].unique()), key="dash_kamoku")
houhou_f = mf2.multiselect("支払方法で絞り込み", sorted(df["支払方法"].unique()), key="dash_houhou")

mat = pivot[show_months].copy()
mat = mat.join(info_cols[["科目", "支払方法", "支払日", "支払元銀行"]])
if kamoku_f:
    mat = mat[mat["科目"].isin(kamoku_f)]
if houhou_f:
    mat = mat[mat["支払方法"].isin(houhou_f)]
mat = mat.sort_values(["科目", "会社名"])

# 増減マーク列
_updown_map = {n: r for n, c, a, d, r in alerts_updown}
mat.insert(0, "増減", [("🔺" if _updown_map[n] > 0 else "🔻") + f"{_updown_map[n]:+.0f}%"
                     if n in _updown_map else "" for n in mat.index])
disp = mat[["増減", "科目", "支払方法", "支払日", "支払元銀行"] + show_months].copy()
for m in show_months:
    disp[m] = disp[m].map(lambda v: f"{int(v):,}" if v else "")
st.dataframe(disp, use_container_width=True, height=520)
totals = pivot[show_months].sum()
st.caption("月合計: " + "　".join(f"{m}: {int(totals[m]):,}円" for m in show_months[-6:]))

# ---------- ③ 銀行別資金必要額 ----------
st.markdown(f"## 🏦 銀行別資金必要額（{target}・口座振替込み）")
cur_df = df[df["対象月"] == target].copy()
if cur_df.empty:
    st.info("対象月の支払データがありません。")
else:
    bank_g = (cur_df.groupby(["支払元銀行", "支払方法"])["金額"].sum()
              .unstack(fill_value=0.0))
    bank_g["合計"] = bank_g.sum(axis=1)
    bank_g = bank_g.sort_values("合計", ascending=False)
    st.dataframe(bank_g.apply(lambda c: c.map(lambda v: f"{int(v):,}")),
                 use_container_width=True)
    st.caption(f"総支払予定: {int(cur_df['金額'].sum()):,}円。"
               "各行の合計がその口座に必要な残高の目安です（不足しそうなら資金移動）。")

    # 銀行×支払日の内訳
    with st.expander("支払日別の内訳（銀行ごと）", expanded=False):
        for bank in bank_g.index:
            sub = cur_df[cur_df["支払元銀行"] == bank]
            day_g = sub.groupby("支払日")["金額"].sum()
            day_g = day_g.reindex(sorted(day_g.index, key=pay_day_key))
            st.markdown(f"**{bank}**（計 {int(sub['金額'].sum()):,}円）: " +
                        "　".join(f"{d or '(未設定)'}: {int(v):,}円"
                                  for d, v in day_g.items()))

# ---------- ④ 資金繰りカレンダー ----------
st.markdown(f"## 📅 資金繰りカレンダー（{target}）")
if not cur_df.empty:
    cal = cur_df.copy()
    cal["日"] = cal["支払日"].map(pay_day_key)
    cal = cal.sort_values(["日", "支払元銀行", "会社名"])
    cal_disp = cal[["支払日", "会社名", "支払方法", "支払元銀行", "科目", "金額"]].copy()
    cal_disp["金額"] = cal_disp["金額"].map(lambda v: f"{int(v):,}")
    st.dataframe(cal_disp, use_container_width=True, hide_index=True)
    # 日別累計（キャッシュアウトの山谷）
    day_sum = cal.groupby("日")["金額"].sum().sort_index()
    day_sum.index = [("末日" if d == 31 else ("不明" if d >= 98 else f"{d}日"))
                     for d in day_sum.index]
    st.bar_chart(day_sum)

# ---------- ⑤ 科目別月次推移 ----------
st.markdown("## 📈 科目別月次推移")
kamoku_pivot = (df[df["対象月"].isin(show_months)]
                .pivot_table(index="対象月", columns="科目", values="金額", aggfunc="sum")
                .fillna(0.0).sort_index())
st.bar_chart(kamoku_pivot)
with st.expander("表で見る", expanded=False):
    st.dataframe(kamoku_pivot.apply(lambda c: c.map(lambda v: f"{int(v):,}")),
                 use_container_width=True)
