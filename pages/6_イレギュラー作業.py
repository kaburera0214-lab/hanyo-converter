# -*- coding: utf-8 -*-
"""
イレギュラー作業入力ページ（[汎用]作業料の元データ）

2つのモード:
  - かんたん入力（現場・既定）: フォームで1件ずつ追加するだけ。過去分は読み取り
    専用で表示され、誤操作で過去を壊す心配がない。
  - 編集・管理（請求担当向け）: テーブルで編集・削除・範囲表示・CSV取込。

請求書発行ページがこの月次合計人時 × 時給単価で [汎用]作業料 を自動算出する。
このページは完全独立。session_stateキーは "irr_"/"invoice_" 接頭辞で分離。
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="イレギュラー作業入力", layout="wide")
st.title("イレギュラー作業入力")
st.caption("日々のイレギュラー作業を記録します。請求書の[汎用]作業料に自動反映されます。")

from lib.invoice import store, notion_store, csv_import

COMMON_ITEMS = ["入庫", "出庫", "出荷", "電話", "梱包", "検品", "セット作業", "その他"]


# --- Notion初期化（請求書ページと同じdb_idsを共有） ---
if not st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", ""):
    st.warning("Notion未設定のため保存できません。Secretsに INVOICE_NOTION_PARENT_PAGE_ID を設定してください。")
    st.stop()

try:
    cached = st.session_state.get("invoice_db_ids")
    if cached and all(k in cached for k in notion_store.DB_SCHEMAS):
        db_ids = cached
    else:
        with st.spinner("Notionデータベースを準備中…"):
            db_ids = notion_store.ensure_databases()
            notion_store.seed_clients_if_empty(db_ids, store.DEFAULT_CLIENTS)
        st.session_state["invoice_db_ids"] = db_ids
except Exception as e:
    st.error(f"Notion初期化に失敗しました: {e}")
    st.stop()

try:
    clients = notion_store.load_clients(db_ids) or store.load_clients()
except Exception:
    clients = store.load_clients()
client_names = list(clients.keys())
today = datetime.date.today()


# --- クライアント・モード選択 ---
cc1, cc2 = st.columns([2, 3])
client_name = cc1.selectbox("クライアント", client_names, key="irr_client")
mode = cc2.radio("モード", ["かんたん入力（日々の記録）", "編集・管理（請求担当者向け）"],
                 horizontal=True, key="irr_mode")

# 時給単価（クライアント別・単価マスタの 出力品名=[汎用]作業料）
hourly = 0.0
try:
    for r in notion_store.load_price_master(db_ids, client_name):
        if "[汎用]作業料" in str(r.get("出力品名", "")):
            hourly = float(r["単価"] or 0)
            break
except Exception:
    pass


def _load_all():
    """このクライアントの全レコードをキャッシュして返す。"""
    all_key = f"irr_all_{client_name}"
    if all_key not in st.session_state:
        try:
            st.session_state[all_key] = notion_store.load_irregular_work(
                db_ids, client_name, None)
        except Exception as e:
            st.session_state[all_key] = []
            st.error(f"読込に失敗: {e}")
    return all_key, st.session_state[all_key]


# ============================================================
# かんたん入力モード（現場向け・追加のみ）
# ============================================================
if mode.startswith("かんたん"):
    st.subheader("➕ 作業を1件追加")
    st.caption("日付・時間・内容を入れて『追加』を押すだけ。過去の記録は触れないので安全です。")

    with st.form("irr_quick", clear_on_submit=True):
        f1, f2, f3 = st.columns(3)
        d = f1.date_input("日付", value=today, key="irr_q_date")
        hours = f2.number_input("時間数(h)", min_value=0.0, step=0.25,
                                value=0.5, key="irr_q_hours")
        people = f3.number_input("人数", min_value=1, step=1, value=1, key="irr_q_people")
        g1, g2 = st.columns(2)
        item_sel = g1.selectbox("作業項目", COMMON_ITEMS, key="irr_q_item")
        item_free = g2.text_input("作業項目（その他の場合は入力）", key="irr_q_itemfree")
        detail = st.text_input("作業詳細", key="irr_q_detail")
        note = st.text_input("備考（任意）", key="irr_q_note")
        submitted = st.form_submit_button("➕ この作業を追加", type="primary")

    if submitted:
        item = item_free.strip() or item_sel
        if hours <= 0:
            st.warning("時間数を入力してください。")
        else:
            try:
                notion_store.add_irregular_work(db_ids, client_name, {
                    "日付": d.strftime("%Y/%m/%d"),
                    "時間数": hours, "人数": people,
                    "作業項目": item, "作業詳細": detail, "備考": note})
                st.session_state.pop(f"irr_all_{client_name}", None)
                st.success(f"追加しました：{d.strftime('%Y/%m/%d')} {item} "
                           f"{hours}h×{people}人")
            except Exception as e:
                st.error(f"追加に失敗しました: {e}")

    # 直近の記録（今月）を読み取り専用で表示
    st.markdown("---")
    cur_ym = f"{today.year}-{today.month:02d}"
    st.markdown(f"#### {client_name}／{cur_ym} の記録（確認用・編集不可）")
    if st.button("🔄 最新に更新", key="irr_q_reload"):
        st.session_state.pop(f"irr_all_{client_name}", None)
        st.rerun()
    _, all_rows = _load_all()
    cur_rows = [r for r in all_rows
                if notion_store._ym_from_date(r.get("日付", ""), "") == cur_ym]
    if cur_rows:
        view = pd.DataFrame(
            [{"日付": r["日付"], "時間数": r["時間数"], "人数": r["人数"],
              "合計時間": r["合計時間"], "作業項目": r["作業項目"],
              "作業詳細": r["作業詳細"], "備考": r["備考"]} for r in cur_rows])
        st.dataframe(view, use_container_width=True, hide_index=True)
        th = sum(float(r.get("合計時間") or 0) for r in cur_rows)
        st.metric("今月の合計人時", f"{th:g} h",
                  help=f"[汎用]作業料概算: {round(th * hourly):,} 円")
    else:
        st.info("今月の記録はまだありません。")
    st.stop()


# ============================================================
# 編集・管理モード（請求担当者向け）
# ============================================================
st.subheader("編集・管理（請求担当者向け）")
st.warning("このモードは過去データの編集・削除ができます。誤操作にご注意ください。")

m_c1, m_c2, m_c3 = st.columns([1, 1, 1])
year = m_c1.number_input("対象年", min_value=2020, max_value=2100,
                         value=today.year, step=1, key="irr_year")
_last_billing = today.month - 1 if today.month > 1 else 12
start_month = m_c2.selectbox("開始月", list(range(1, 13)),
                             index=_last_billing - 1, key="irr_start")
end_month = m_c3.selectbox("終了月", list(range(1, 13)),
                           index=_last_billing - 1, key="irr_end")
show_all = st.checkbox("全期間を表示（年月指定を無視してこのクライアントの全月）",
                       key="irr_showall")
if start_month > end_month:
    start_month, end_month = end_month, start_month
range_yms = {f"{int(year)}-{m:02d}" for m in range(start_month, end_month + 1)}
fallback_ym = f"{int(year)}-{end_month:02d}"
scope_sig = "ALL" if show_all else f"{int(year)}-{start_month:02d}_{end_month:02d}"
st.caption(f"時給単価: {hourly:,.0f} 円/h（「請求書発行」→単価マスタ管理で変更できます）")

all_key = f"irr_all_{client_name}"
if st.button("🔄 データを再読込", key="irr_reload"):
    st.session_state.pop(all_key, None)
    st.rerun()
_, all_rows = _load_all()


def _eff_ym(r):
    return notion_store._ym_from_date(r.get("日付", ""), r.get("対象年月", ""))


with st.expander(f"📊 保存済みデータの月別件数（全{len(all_rows)}件）", expanded=False):
    _diag = {}
    for r in all_rows:
        em = _eff_ym(r)
        _diag.setdefault(em, [0, 0.0])
        _diag[em][0] += 1
        _diag[em][1] += float(r.get("合計時間") or 0)
    if _diag:
        st.dataframe(pd.DataFrame(
            [{"対象年月(日付基準)": k, "件数": v[0], "合計人時": f"{v[1]:g} h"}
             for k, v in sorted(_diag.items())]),
            use_container_width=True, hide_index=True)
    else:
        st.info("このクライアントの保存データはありません。")

if show_all:
    existing = all_rows
else:
    existing = [r for r in all_rows if _eff_ym(r) in range_yms]

base_df = pd.DataFrame(
    [{"id": r.get("id", ""), "日付": r["日付"], "時間数": r["時間数"], "人数": r["人数"],
      "作業項目": r["作業項目"], "作業詳細": r["作業詳細"], "備考": r["備考"]}
     for r in existing],
    columns=["id", "日付", "時間数", "人数", "作業項目", "作業詳細", "備考"])
loaded_ids = [r["id"] for r in existing if r.get("id")]

with st.expander("スプレッドシートCSVから取込（移行用）", expanded=False):
    st.caption("列：日付 / 時間数(h) / 人数 / 作業項目 / 作業詳細 / 備考。取り込むと下の表に追加されます。")
    up = st.file_uploader("イレギュラー作業CSV", type=["csv"], key="irr_csv")
    if up is not None:
        try:
            imported = csv_import.parse_irregular_csv(up.getvalue())
            imp_df = pd.DataFrame(imported, columns=base_df.columns)
            base_df = pd.concat([base_df, imp_df], ignore_index=True)
            st.success(f"{len(imp_df)} 行を取り込みました（下の表で確認し『保存』してください）。")
        except Exception as e:
            st.error(f"CSV取込に失敗: {e}")

_scope_label = "全期間" if show_all else f"{int(year)}-{start_month:02d}〜{end_month:02d}"
st.markdown(f"#### {client_name} の作業記録（{_scope_label}）")
st.caption(f"日付が読めない行の保存先は {fallback_ym}。各行は『日付』の月に振り分けて保存されます。"
           "　行を消して保存すると、その記録は削除されます（表示中の範囲のみ対象）。")
edited = st.data_editor(
    base_df,
    num_rows="dynamic",
    use_container_width=True,
    key=f"irr_editor_{client_name}_{scope_sig}",
    column_config={
        "id": None,
        "日付": st.column_config.TextColumn("日付", help="例: 2026/04/14"),
        "時間数": st.column_config.NumberColumn("時間数(h)", step=0.25, min_value=0),
        "人数": st.column_config.NumberColumn("人数", step=1, min_value=0),
        "作業項目": st.column_config.TextColumn("作業項目", help="入庫/出庫/電話 など"),
        "作業詳細": st.column_config.TextColumn("作業詳細", width="large"),
        "備考": st.column_config.TextColumn("備考"),
    },
)

edited2 = edited.copy()
edited2["合計時間"] = (pd.to_numeric(edited2["時間数"], errors="coerce").fillna(0)
                    * pd.to_numeric(edited2["人数"], errors="coerce").fillna(0))
total_hours = float(edited2["合計時間"].sum())

m1, m2, m3 = st.columns(3)
m1.metric("合計人時（表全体）", f"{total_hours:g} h")
m2.metric("時給単価", f"{hourly:,.0f} 円/h")
m3.metric("[汎用]作業料（表全体・概算）", f"{round(total_hours * hourly):,} 円")

edited2["対象年月"] = edited2["日付"].map(lambda d: notion_store._ym_from_date(d, fallback_ym))
by_month = edited2.groupby("対象年月")["合計時間"].sum()
if len(by_month) >= 1:
    st.caption("月ごとの合計人時：")
    st.dataframe(
        pd.DataFrame([{"対象年月": k, "合計人時": f"{v:g} h",
                       "[汎用]作業料概算": f"{round(v * hourly):,} 円"}
                      for k, v in by_month.items()]),
        use_container_width=True, hide_index=True)

if st.button("💾 保存", key="irr_save", type="primary"):
    try:
        res = notion_store.save_irregular_work(
            db_ids, client_name, edited.to_dict("records"), loaded_ids, fallback_ym)
        st.session_state.pop(all_key, None)
        st.success(
            f"{client_name} の作業記録を保存しました"
            f"（新規{res['created']}・更新{res['updated']}・削除{res['deleted']}）。"
            "請求書発行ページの[汎用]作業料に反映されます。")
    except Exception as e:
        st.error(f"保存に失敗しました: {e}")
