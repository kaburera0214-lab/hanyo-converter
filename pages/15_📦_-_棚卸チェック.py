# -*- coding: utf-8 -*-
"""
棚卸チェック（資材備品）

有効な全資材を保管ロケーション（棚卸グループ）ごとに表示し、現在庫を入力する。
  - 発注点がある資材 → 現在庫 <= 発注点 で自動的に「要発注」抽出
                        発注数量は (在庫定数−現在庫) をロット単位に切上げて提案（手修正可）
  - 発注点がない資材（都度確認）→ 最後の確認工程に集約。チェック者が1件ずつ
                        「確認済」にし、必要なら「発注する」を選ぶ（必ず確認させる）
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="棚卸チェック", layout="wide")
st.title("📦 棚卸チェック")
st.caption("有効な全資材の現在庫を入力 → 発注点以下を『要発注』として抽出。発注点のない資材は最後に必ず確認します。")

from lib.material import app_init, notion_material as N, ordering as O

JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst():
    return datetime.datetime.now(JST)


def has_reorder_point(v):
    s = str(v).strip()
    return s != "" and s.lower() not in ("nan", "none")


try:
    db_ids = app_init.init_material()
except Exception as e:  # noqa: BLE001
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

if st.button("🔄 マスタを再読込", key="st_reload"):
    for k in ("st_master", "st_result", "st_order_df", "st_order_nonce",
              "st_order_qty", "st_order_lot", "st_lastcounts"):
        st.session_state.pop(k, None)

if "st_master" not in st.session_state:
    st.session_state["st_master"] = N.load_master(db_ids)
master = [r for r in st.session_state["st_master"]
          if str(r.get("有効フラグ", "")).strip() in ("✓", "1", "True", "true", "○")]

if not master:
    st.info("有効な資材がありません。先に「📦 資材マスタ」で資材を登録し、有効フラグを✓にしてください。")
    st.stop()

auto_items = [r for r in master if has_reorder_point(r.get("発注点"))]
manual_items = [r for r in master if not has_reorder_point(r.get("発注点"))]

# ロット候補と単価の個数不一致を警告（ロット変更＝価格変更の取りこぼし防止）
mismatched = [r for r in master if not O.lot_price_ok(r.get("ロット候補"), r.get("単価"))]
if mismatched:
    st.warning("⚠️ ロット候補と単価の個数が一致しない資材があります（資材マスタで修正してください）："
               + "、".join(r["資材名"] for r in mismatched[:15]))

# 前回の棚卸値（資材名→{現在庫, 棚卸日}）。減少量の表示に使う。
if "st_lastcounts" not in st.session_state:
    try:
        st.session_state["st_lastcounts"] = N.last_counts(db_ids)
    except Exception:  # noqa: BLE001
        st.session_state["st_lastcounts"] = {}
last = st.session_state["st_lastcounts"]


def _days_since(day_str):
    """『YYYY-MM-DD HH:MM』等から現在(JST)までの日数。失敗時None。"""
    s = (day_str or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(s, fmt).replace(tzinfo=JST)
            return max((now_jst() - dt).days, 0)
        except ValueError:
            continue
    return None


st.markdown(f"### ① 現在庫の入力（発注点あり {len(auto_items)}件）")
st.caption("資材ごとに現在庫を入力（前回値・発注点を表示）。最後に『要発注を判定』でまとめて確定します。"
           "初期値は前回値です。減ったぶんだけ数を下げてください。")

locations = sorted({(r.get("保管ロケーション") or "（未設定）") for r in auto_items})
with st.form("st_input_form"):
    for loc in locations:
        grp = [r for r in auto_items if (r.get("保管ロケーション") or "（未設定）") == loc]
        st.markdown(f"**📍 {loc}**（{len(grp)}件）")
        for r in grp:
            prev = last.get(r["資材名"], {})
            pv = prev.get("現在庫")
            meta = []
            if pv is not None:
                dsince = _days_since(prev.get("棚卸日"))
                meta.append(f"前回 {O.to_num(pv):g}" + (f"・{dsince}日前" if dsince is not None else ""))
            meta.append(f"発注点 {O.to_num(r.get('発注点')):g}")
            lot = str(r.get("ロット候補", "")).strip()
            if lot:
                meta.append(f"ロット {lot}")
            st.number_input(
                f"{r['資材名']}　〔{' / '.join(meta)}〕",
                min_value=0.0, step=1.0,
                value=float(O.to_num(pv)) if pv is not None else 0.0,
                key=f"cur_{r['id']}")
    submitted = st.form_submit_button("🧮 要発注を判定", type="primary",
                                      use_container_width=True)

if submitted:
    detail = []
    for r in auto_items:
        loc = r.get("保管ロケーション") or "（未設定）"
        cur = st.session_state.get(f"cur_{r['id']}", 0.0)
        lot_str = r.get("ロット候補", "")
        prev = last.get(r["資材名"], {})
        pv = prev.get("現在庫")
        rec = {
            "id": r["id"], "資材名": r["資材名"], "ロケーション": loc,
            "現在庫": cur, "発注点": r.get("発注点", ""),
            "在庫定数": r.get("在庫定数", ""), "ロット候補": lot_str,
            "既定ロット": O.default_lot(lot_str), "未入力": False,
            "前回在庫": pv,
            "前回日数": _days_since(prev.get("棚卸日")) if pv is not None else None,
            "減少": (O.to_num(pv) - O.to_num(cur)) if pv is not None else None,
        }
        rec["要発注"] = O.needs_order(rec["現在庫"], rec["発注点"])
        rec["発注数量"] = O.suggest_qty(rec["現在庫"], rec["在庫定数"],
                                    rec["既定ロット"]) if rec["要発注"] else 0
        detail.append(rec)
    st.session_state["st_result"] = detail
    # 要発注の作業用データフレームを初期化（既定ロットでの提案値）
    st.session_state["st_order_df"] = pd.DataFrame([{
        "ロケーション": d["ロケーション"], "資材名": d["資材名"],
        "現在庫": O.to_num(d["現在庫"]), "発注点": O.to_num(d["発注点"]),
        "在庫定数": O.to_num(d["在庫定数"]), "ロット候補": d["ロット候補"],
        "適用ロット": d["既定ロット"], "発注数量": d["発注数量"],
    } for d in detail if d["要発注"]])
    st.session_state["st_order_nonce"] = st.session_state.get("st_order_nonce", 0) + 1

result = st.session_state.get("st_result")
orders = []
if result:
    not_input = [d for d in result if d["未入力"]]
    orders = [d for d in result if d["要発注"]]
    if not_input:
        st.warning(f"現在庫が未入力 {len(not_input)}件：{'、'.join(d['資材名'] for d in not_input[:10])}"
                   + ("…" if len(not_input) > 10 else ""))

    st.markdown(f"### ② 要発注（自動抽出 {len(orders)}件）")
    if not orders:
        st.success("発注点を下回った資材はありません。")
    else:
        st.caption("ロットをまとめ買いしたい場合は『適用ロット』を変えて『発注数量を再計算』を押してください。")
        odf = st.session_state["st_order_df"]
        nonce = st.session_state.get("st_order_nonce", 0)
        oedit = st.data_editor(
            odf, use_container_width=True, hide_index=True, key=f"st_order_editor_{nonce}",
            column_config={
                "ロケーション": st.column_config.TextColumn(disabled=True),
                "資材名": st.column_config.TextColumn(disabled=True),
                "現在庫": st.column_config.NumberColumn(disabled=True),
                "発注点": st.column_config.NumberColumn(disabled=True),
                "在庫定数": st.column_config.NumberColumn(disabled=True),
                "ロット候補": st.column_config.TextColumn("ロット候補", disabled=True),
                "適用ロット": st.column_config.NumberColumn("適用ロット", min_value=0,
                                                   help="まとめ買い時はロット候補のいずれかに変更"),
                "発注数量": st.column_config.NumberColumn("発注数量（提案・手修正可）", min_value=0),
            },
        )
        if st.button("🔁 適用ロットで発注数量を再計算", key="st_recalc"):
            recalced = oedit.copy()
            recalced["発注数量"] = [
                O.suggest_qty(row["現在庫"], row["在庫定数"], row["適用ロット"])
                for _, row in recalced.iterrows()]
            st.session_state["st_order_df"] = recalced
            st.session_state["st_order_nonce"] = nonce + 1
            st.rerun()
        # 表示中の編集内容を作業用DFへ反映し、確定値を保持
        st.session_state["st_order_df"] = oedit
        st.session_state["st_order_qty"] = {row["資材名"]: int(row["発注数量"])
                                            for _, row in oedit.iterrows()}
        st.session_state["st_order_lot"] = {row["資材名"]: O.to_num(row["適用ロット"])
                                            for _, row in oedit.iterrows()}

    # 前回からの減り（消費ペース）。前回値がある資材のみ。
    consumed = [d for d in result if d.get("前回在庫") is not None and d.get("減少") is not None]
    if consumed:
        with st.expander(f"📉 前回からの減り（{len(consumed)}件）", expanded=False):
            crows = []
            for d in sorted(consumed, key=lambda x: (x["減少"] or 0), reverse=True):
                days = d.get("前回日数")
                per = (d["減少"] / days) if (days and days > 0) else None
                crows.append({
                    "資材名": d["資材名"], "前回": O.to_num(d["前回在庫"]),
                    "今回": O.to_num(d["現在庫"]), "減少": d["減少"],
                    "日数": days if days is not None else "—",
                    "1日あたり": round(per, 2) if per is not None else "—",
                })
            st.dataframe(pd.DataFrame(crows), use_container_width=True, hide_index=True)

# --- ③ 都度確認（発注点なし）：最後の確認工程 ---
st.markdown("---")
st.markdown(f"### ③ 都度確認（発注点なし {len(manual_items)}件）")
st.caption("数値で自動判定できない資材です。1件ずつ「確認済」にし、必要なら「発注する」を選んでください（全件の確認が必要です）。")

mdf = pd.DataFrame([{
    "id": r["id"], "ロケーション": r.get("保管ロケーション", ""), "カテゴリ": r.get("カテゴリ", ""),
    "資材名": r["資材名"], "備考": r.get("備考", ""),
    "確認済": False, "発注する": False, "発注数量": 0, "メモ": "",
} for r in manual_items])
medit = st.data_editor(
    mdf, use_container_width=True, hide_index=True, key="st_manual_editor",
    column_config={
        "id": None,
        "ロケーション": st.column_config.TextColumn("ロケーション", disabled=True),
        "カテゴリ": st.column_config.TextColumn("分類", disabled=True, width="small"),
        "資材名": st.column_config.TextColumn("資材名", disabled=True),
        "備考": st.column_config.TextColumn("内容・条件", disabled=True, width="large"),
        "確認済": st.column_config.CheckboxColumn("確認済", help="この資材を確認したらチェック"),
        "発注する": st.column_config.CheckboxColumn("発注する"),
        "発注数量": st.column_config.NumberColumn("発注数量", min_value=0),
        "メモ": st.column_config.TextColumn("メモ"),
    },
)

# --- ④ プレビュー＆保存 ---
st.markdown("---")
manual_unchecked = int((~medit["確認済"]).sum()) if len(medit) else 0
manual_orders = [row for _, row in medit.iterrows() if row.get("発注する")]
order_qty = st.session_state.get("st_order_qty", {})

# 仕入先（ロケーション）ごとの発注プレビュー
combined = []
for d in orders:
    combined.append({"ロケーション": d["ロケーション"], "資材名": d["資材名"],
                     "発注数量": order_qty.get(d["資材名"], d["発注数量"])})
for row in manual_orders:
    combined.append({"ロケーション": row.get("ロケーション", ""), "資材名": row["資材名"],
                     "発注数量": int(row.get("発注数量") or 0)})

if combined:
    with st.expander(f"📋 発注プレビュー（ロケーション別 計{len(combined)}件）", expanded=True):
        pdf = pd.DataFrame(combined)
        for loc, g in pdf.groupby("ロケーション"):
            lines = [f"- {r['資材名']}：{int(r['発注数量'])}" for _, r in g.iterrows()]
            st.markdown(f"**{loc or '（未設定）'}**\n" + "\n".join(lines))

if manual_unchecked:
    st.warning(f"都度確認が未確認 {manual_unchecked}件あります。全件「確認済」にしてから保存してください。")

if st.button("💾 この棚卸を保存", key="st_save", disabled=(result is None)):
    if result is None:
        st.error("先に「要発注を判定」を実行してください。")
    elif manual_unchecked:
        st.error(f"都度確認が {manual_unchecked}件 未確認です。全件確認してください。")
    else:
        order_lot = st.session_state.get("st_order_lot", {})
        save_detail = []
        for d in result:
            if d["未入力"]:
                continue
            save_detail.append({
                "資材名": d["資材名"], "ロケーション": d["ロケーション"], "判定方式": "自動",
                "現在庫": O.to_num(d["現在庫"]), "発注点": O.to_num(d["発注点"]),
                "在庫定数": O.to_num(d["在庫定数"]),
                "適用ロット": order_lot.get(d["資材名"], d["既定ロット"]),
                "前回在庫": d.get("前回在庫"), "前回日数": d.get("前回日数"),
                "減少": d.get("減少"),
                "要発注": bool(d["要発注"]),
                "発注数量": order_qty.get(d["資材名"], d["発注数量"]) if d["要発注"] else 0,
            })
        for _, row in medit.iterrows():
            save_detail.append({
                "資材名": row["資材名"], "ロケーション": row.get("ロケーション", ""),
                "判定方式": "都度確認", "確認済": bool(row.get("確認済")),
                "要発注": bool(row.get("発注する")),
                "発注数量": int(row.get("発注数量") or 0) if row.get("発注する") else 0,
                "メモ": str(row.get("メモ") or ""),
            })
        try:
            N.save_stocktake(db_ids, 棚卸日=now_jst().strftime("%Y-%m-%d %H:%M"),
                             明細=save_detail)
            n_order = sum(1 for d in save_detail if d.get("要発注"))
            st.session_state.pop("st_lastcounts", None)  # 次回の前回値に反映
            st.success(f"棚卸を保存しました（明細{len(save_detail)}件・要発注{n_order}件）。")
        except Exception as e:  # noqa: BLE001
            st.error(f"保存に失敗しました: {e}")

# --- 履歴 ---
st.markdown("---")
with st.expander("🕘 過去の棚卸履歴", expanded=False):
    try:
        hist = N.load_stocktakes(db_ids)
    except Exception as e:  # noqa: BLE001
        hist = []
        st.warning(f"履歴の取得に失敗: {e}")
    if not hist:
        st.caption("履歴はまだありません。")
    for h in hist[:20]:
        st.markdown(f"**{h['棚卸日']}** — 明細{int(h['明細件数'])}件 / 要発注{int(h['要発注件数'])}件")
