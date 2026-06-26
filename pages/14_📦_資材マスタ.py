# -*- coding: utf-8 -*-
"""
資材マスタ（資材備品）

資材名・品番・仕入先・ロット・単価・発注点・在庫定数を管理する。
仕入先の口座/メール宛先は買掛の取引先マスタ（支払_）を参照するため、
ここでは紐付けキー（NE仕入先cd）と表示用の仕入先名だけを持つ。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="資材マスタ", layout="wide")
st.title("📦 資材マスタ")
st.caption("資材備品の発注点・在庫定数・仕入先（NE仕入先cd連携）を管理します。")

from lib.material import app_init, notion_material as N

try:
    db_ids = app_init.init_material()
except Exception as e:  # noqa: BLE001
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

# 仕入先候補（買掛の取引先マスタから。空でも手入力可）
suppliers = app_init.load_supplier_options()
sup_codes = [""] + [s["NE仕入先cd"] for s in suppliers if s["NE仕入先cd"]]
code2name = {s["NE仕入先cd"]: s["会社名"] for s in suppliers if s["NE仕入先cd"]}

rc1, rc2 = st.columns([1, 3])
if rc1.button("🔄 最新に更新", key="mm_reload"):
    st.session_state.pop("mm_rows", None)
if rc2.button("🏷 仕入先名をNE仕入先cdから補完", key="mm_fill_name",
              help="NE仕入先cdに対応する会社名を取引先マスタから埋めます"):
    if not code2name:
        st.warning("取引先マスタ（買掛）から仕入先を取得できませんでした。")
    else:
        rows = st.session_state.get("mm_rows") or N.load_master(db_ids)
        updated = 0
        for r in rows:
            cd = (r.get("NE仕入先cd") or "").strip()
            nm = code2name.get(cd, "")
            if nm and r.get("仕入先名", "") != nm and r.get("id"):
                N.upsert_master_row(db_ids, {**r, "仕入先名": nm})
                updated += 1
        st.session_state.pop("mm_rows", None)
        st.success(f"{updated}件の仕入先名を補完しました。")

if "mm_rows" not in st.session_state:
    st.session_state["mm_rows"] = N.load_master(db_ids)
rows = st.session_state["mm_rows"]

st.markdown(f"### 登録済み {len(rows)}件")
kw = st.text_input("資材名・品番・仕入先で絞り込み（空欄で全件）", key="mm_kw").strip()
def _hit(r):
    if not kw:
        return True
    return any(kw in str(r.get(c, "")) for c in ("資材名", "品番", "仕入先名", "NE仕入先cd"))
view = [r for r in rows if _hit(r)]

# ロット候補と単価の個数が合わない行を警告（ロット変更＝価格変更の取りこぼし防止）
from lib.material import ordering as O
mismatched = [r for r in rows if not O.lot_price_ok(r.get("ロット候補"), r.get("単価"))]
if mismatched:
    st.warning("⚠️ ロット候補と単価の個数が一致しない資材があります（ロットを増やしたら単価も対応させてください）：\n"
               + "\n".join(f"- {r['資材名']}：ロット候補「{r.get('ロット候補','')}」／単価「{r.get('単価','')}」"
                           for r in mismatched[:20]))

edit_cols = ["id", "資材名", "品番", "カテゴリ", "NE仕入先cd", "仕入先名", "発注方法",
             "ロット候補", "単価", "発注点", "在庫定数", "保管ロケーション", "有効フラグ", "備考"]
df = pd.DataFrame(view)
for c in edit_cols:
    if c not in df.columns:
        df[c] = ""
df = df[edit_cols]

edited = st.data_editor(
    df, use_container_width=True, num_rows="dynamic", key="mm_editor",
    column_config={
        "id": st.column_config.TextColumn("id", disabled=True, width="small"),
        "カテゴリ": st.column_config.TextColumn("カテゴリ", help="先頭の括弧（段/ワ/プチ/プ/日/倉庫/廃止候補/パウチ参照 等）"),
        "保管ロケーション": st.column_config.TextColumn(
            "保管ロケーション", help="棚卸グループ（ロッテ/カープ/トイプー：備品棚/梱包室/事務所備品 等）"),
        "NE仕入先cd": st.column_config.SelectboxColumn(
            "NE仕入先cd", options=sup_codes,
            help="買掛の取引先マスタの仕入先cd（例 n001）。発注先口座と連携") if len(sup_codes) > 1
            else st.column_config.TextColumn("NE仕入先cd", help="買掛の仕入先cd（例 n001）"),
        "仕入先名": st.column_config.TextColumn("仕入先名", help="表示用。cdから補完できます"),
        "発注方法": st.column_config.SelectboxColumn(
            "発注方法", options=["", "メール発注", "社内チャット依頼", "FAX発注"]),
        "ロット候補": st.column_config.TextColumn(
            "ロット候補", help="発注単位。カンマ区切りで複数可（全/半角OK）。先頭＝既定。例 20,100"),
        "単価": st.column_config.TextColumn(
            "単価", help="ロット候補に対応してカンマ区切り。例 1400,6500（1つだけなら全ロット共通）"),
        "発注点": st.column_config.NumberColumn("発注点", help="この在庫数以下で要発注", min_value=0),
        "在庫定数": st.column_config.NumberColumn("在庫定数", help="あるべき基準在庫", min_value=0),
        "有効フラグ": st.column_config.TextColumn("有効", help="✓で棚卸対象。空欄は対象外"),
    },
)

if st.button("💾 変更を保存", type="primary", key="mm_save"):
    orig = {r["id"]: r for r in st.session_state.get("mm_rows", []) if r.get("id")}
    created = updated = 0
    for _, r in edited.iterrows():
        rec = {k: ("" if pd.isna(r.get(k)) else r.get(k)) for k in edit_cols}
        if not str(rec["資材名"]).strip():
            continue
        # 仕入先名が空でcdが分かるなら自動補完
        cd = str(rec.get("NE仕入先cd", "")).strip()
        if cd and not str(rec.get("仕入先名", "")).strip() and code2name.get(cd):
            rec["仕入先名"] = code2name[cd]
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
            st.error(f"{rec['資材名']} の保存に失敗: {e}")
    st.session_state.pop("mm_rows", None)
    st.success(f"新規{created}件・更新{updated}件を保存しました。")
    st.rerun()

st.markdown("---")
with st.expander("ℹ️ 使い方", expanded=False):
    st.markdown(
        "- **発注点**：現在庫がこの数**以下**になったら『要発注』として抽出されます。空欄の資材は棚卸の『都度確認』工程に回ります。\n"
        "- **在庫定数**：あるべき基準在庫。発注数量は『(在庫定数−現在庫)をロット単位に切上げ』で提案します。\n"
        "- **ロット候補**：発注単位。`20,100` のようにカンマ区切り（全/半角OK）で複数登録でき、先頭が既定。発注時にプルダウンで選べます。\n"
        "- **単価**：ロット候補に対応してカンマ区切り（例 `1400,6500`）。1つだけなら全ロット共通単価。ロット候補と個数が合わないと上部に警告が出ます。\n"
        "- **発注方法**：メール発注／社内チャット依頼／FAX発注。`ワ`（社内ネット購入）は社内チャット依頼です。\n"
        "- **NE仕入先cd**：買掛の取引先マスタの仕入先cd（例 n001）を選ぶと、発注先の口座・宛先と連携できます。\n"
        "- **有効フラグ**：`✓` の資材だけが棚卸チェックの対象になります。"
    )
