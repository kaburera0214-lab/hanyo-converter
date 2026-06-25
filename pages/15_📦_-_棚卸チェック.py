# -*- coding: utf-8 -*-
"""
棚卸チェック（資材備品）

有効な全資材を1画面に表示し、現在庫数を入力していく。
入力後『要発注を判定』で 現在庫 <= 発注点 の資材を抽出し、
発注数量の提案値（(在庫定数−現在庫)をロット単位に切上げ）を表示する。
提案値は画面で手修正でき、棚卸結果としてNotionに保存できる。
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="棚卸チェック", layout="wide")
st.title("📦 棚卸チェック")
st.caption("有効な全資材の現在庫を入力 → 発注点以下を『要発注』として抽出します。")

from lib.material import app_init, notion_material as N, ordering as O

JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst():
    return datetime.datetime.now(JST)


try:
    db_ids = app_init.init_material()
except Exception as e:  # noqa: BLE001
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

if st.button("🔄 マスタを再読込", key="st_reload"):
    st.session_state.pop("st_master", None)
    st.session_state.pop("st_input", None)

if "st_master" not in st.session_state:
    st.session_state["st_master"] = N.load_master(db_ids)
master = [r for r in st.session_state["st_master"]
          if str(r.get("有効フラグ", "")).strip() in ("✓", "1", "True", "true", "○")]

if not master:
    st.info("有効な資材がありません。先に「📦 資材マスタ」で資材を登録し、有効フラグを✓にしてください。")
    st.stop()

st.markdown(f"### 対象資材 {len(master)}件（有効のみ）")

# --- 現在庫入力テーブル ---
base = []
for r in master:
    base.append({
        "id": r["id"],
        "資材名": r["資材名"],
        "仕入先名": r.get("仕入先名", ""),
        "保管ロケーション": r.get("保管ロケーション", ""),
        "現在庫": "",
        "発注点": r.get("発注点", ""),
        "在庫定数": r.get("在庫定数", ""),
        "ロット": r.get("ロット", ""),
    })
df = pd.DataFrame(base)

edited = st.data_editor(
    df, use_container_width=True, hide_index=True, key="st_editor",
    column_config={
        "id": None,
        "資材名": st.column_config.TextColumn("資材名", disabled=True),
        "仕入先名": st.column_config.TextColumn("仕入先名", disabled=True),
        "保管ロケーション": st.column_config.TextColumn("ロケーション", disabled=True),
        "現在庫": st.column_config.NumberColumn("現在庫（入力）", min_value=0,
                                          help="棚卸でカウントした現在の在庫数"),
        "発注点": st.column_config.NumberColumn("発注点", disabled=True),
        "在庫定数": st.column_config.NumberColumn("在庫定数", disabled=True),
        "ロット": st.column_config.NumberColumn("ロット", disabled=True),
    },
)

st.markdown("---")
if st.button("🧮 要発注を判定", type="primary", key="st_judge"):
    detail = []
    for _, r in edited.iterrows():
        cur = r.get("現在庫")
        rec = {
            "id": r["id"],
            "資材名": r["資材名"],
            "仕入先名": r.get("仕入先名", ""),
            "現在庫": "" if pd.isna(cur) else cur,
            "発注点": r.get("発注点", ""),
            "在庫定数": r.get("在庫定数", ""),
            "ロット": r.get("ロット", ""),
            "未入力": pd.isna(cur) or str(cur).strip() == "",
        }
        rec["要発注"] = (not rec["未入力"]) and O.needs_order(rec["現在庫"], rec["発注点"])
        rec["発注数量"] = O.suggest_qty(rec["現在庫"], rec["在庫定数"], rec["ロット"]) if rec["要発注"] else 0
        detail.append(rec)
    st.session_state["st_result"] = detail

result = st.session_state.get("st_result")
if result:
    not_input = [d for d in result if d["未入力"]]
    orders = [d for d in result if d["要発注"]]
    if not_input:
        st.warning(f"未入力 {len(not_input)}件：{'、'.join(d['資材名'] for d in not_input[:10])}"
                   + ("…" if len(not_input) > 10 else ""))

    st.markdown(f"### 要発注 {len(orders)}件")
    if not orders:
        st.success("発注が必要な資材はありません。")
    else:
        odf = pd.DataFrame([{
            "資材名": d["資材名"], "仕入先名": d["仕入先名"],
            "現在庫": O.to_num(d["現在庫"]), "発注点": O.to_num(d["発注点"]),
            "在庫定数": O.to_num(d["在庫定数"]), "ロット": O.to_num(d["ロット"]),
            "発注数量": d["発注数量"],
        } for d in orders])
        oedit = st.data_editor(
            odf, use_container_width=True, hide_index=True, key="st_order_editor",
            column_config={
                "資材名": st.column_config.TextColumn(disabled=True),
                "仕入先名": st.column_config.TextColumn(disabled=True),
                "現在庫": st.column_config.NumberColumn(disabled=True),
                "発注点": st.column_config.NumberColumn(disabled=True),
                "在庫定数": st.column_config.NumberColumn(disabled=True),
                "ロット": st.column_config.NumberColumn(disabled=True),
                "発注数量": st.column_config.NumberColumn("発注数量（提案・手修正可）", min_value=0),
            },
        )
        # 仕入先ごとの集計プレビュー（Phase2の発注書作成の単位）
        with st.expander("📋 仕入先ごとの発注プレビュー", expanded=True):
            for sup, g in oedit.groupby("仕入先名"):
                lines = [f"- {row['資材名']}：{int(row['発注数量'])}" for _, row in g.iterrows()]
                st.markdown(f"**{sup or '（仕入先未設定）'}**\n" + "\n".join(lines))

        # 棚卸結果の保存（手修正後の発注数量を反映）
        if st.button("💾 この棚卸を保存", key="st_save"):
            qty_map = {row["資材名"]: int(row["発注数量"]) for _, row in oedit.iterrows()}
            save_detail = []
            for d in result:
                if d["未入力"]:
                    continue
                save_detail.append({
                    "資材名": d["資材名"], "仕入先名": d["仕入先名"],
                    "現在庫": O.to_num(d["現在庫"]),
                    "発注点": O.to_num(d["発注点"]),
                    "在庫定数": O.to_num(d["在庫定数"]),
                    "ロット": O.to_num(d["ロット"]),
                    "要発注": bool(d["要発注"]),
                    "発注数量": qty_map.get(d["資材名"], d["発注数量"]) if d["要発注"] else 0,
                })
            try:
                N.save_stocktake(db_ids, 棚卸日=now_jst().strftime("%Y-%m-%d %H:%M"),
                                 明細=save_detail)
                st.success(f"棚卸を保存しました（明細{len(save_detail)}件・要発注{len(orders)}件）。")
            except Exception as e:  # noqa: BLE001
                st.error(f"保存に失敗しました: {e}")

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
