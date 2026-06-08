# -*- coding: utf-8 -*-
"""
保管カウント入力ページ（保管料の元データ）

明細行ベース：1行＝カウント日・種別・ロケーション・数量・備考。
種別ごとに複数ロケーションの行を持てる。請求時に種別ごとの2期平均×単価で保管料を算出。

2つのモード（イレギュラー作業と同じ思想）:
  - かんたん入力（現場・既定）: 1行ずつフォームで追加。過去は読み取り専用。
  - 編集・管理（請求担当向け）: テーブルで編集・削除（確認つき）・CSV取込。
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="保管カウント入力", layout="wide")
st.title("保管カウント入力")
st.caption("月2回（第1期=15日頃／第2期=末日頃）の在庫を、種別・ロケーションごとに記録します。")

from lib.invoice import store, notion_store, csv_import


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

cc1, cc2 = st.columns([2, 3])
_def_idx = client_names.index("Team-EC") if "Team-EC" in client_names else 0
client_name = cc1.selectbox("クライアント", client_names, index=_def_idx, key="stk_client")
mode = cc2.radio("モード", ["かんたん入力（カウント記録）", "編集・管理（請求担当者向け）"],
                 horizontal=True, key="stk_mode")

master = clients.get(client_name, {}).get("保管料マスタ", [])
master_names = [m["種別名"] for m in master]
master_price = {m["種別名"]: m["単価"] for m in master}
master_out = {m["種別名"]: m["出力品名"] for m in master}


def _load(ym):
    key = f"stk_lines_{client_name}_{ym}"
    if key not in st.session_state:
        try:
            st.session_state[key] = notion_store.load_storage_counts(db_ids, client_name, ym)
        except Exception as e:
            st.session_state[key] = []
            st.error(f"読込に失敗: {e}")
    return key, st.session_state[key]


def _ym_of(d):
    return f"{d.year}-{d.month:02d}"


def _show_summary(ym):
    _, rows = _load(ym)
    prev, by_out, warns = notion_store.aggregate_storage(rows, master_price, master_out)
    if prev:
        st.dataframe(pd.DataFrame(prev), use_container_width=True, hide_index=True)
        st.metric("保管料 合計", f"{sum(by_out.values()):,} 円")
    else:
        st.info("まだカウントがありません。")
    for w in warns:
        st.warning(w)


# ============================================================
# かんたん入力モード（1行ずつ追加）
# ============================================================
if mode.startswith("かんたん"):
    s1, s2, s3 = st.columns([1, 1, 2])
    year = s1.number_input("対象年", 2020, 2100, value=today.year, step=1, key="stk_q_year")
    month = s2.selectbox("対象月", list(range(1, 13)), index=today.month - 1, key="stk_q_month")
    period = s3.radio("カウント期", ["第1期（15日）", "第2期（末日）"],
                      horizontal=True, key="stk_q_period")
    period_name = "第1期" if period.startswith("第1") else "第2期"
    ym = f"{int(year)}-{int(month):02d}"
    cdate = st.date_input("カウント日（この期の全行に付きます）", value=today, key="stk_q_date")

    if st.button("🔄 最新に更新", key="stk_q_reload"):
        st.session_state.pop(f"stk_lines_{client_name}_{ym}", None)
        st.rerun()
    _, all_rows = _load(ym)
    period_rows = [r for r in all_rows if r["期"] == period_name]
    loaded_ids = [r["id"] for r in period_rows if r.get("id")]

    st.markdown(f"#### {client_name}／{ym}／{period_name} の入力")
    st.caption("種別ごとに行を足して、ロケーション・数量・備考を入力してください。"
               "同じ種別を複数行（ロケーション違い）書けます。選んだ期だけ更新されます。")
    sheet = pd.DataFrame(
        [{"id": r.get("id", ""), "種別": r["種別"], "ロケーション": r["ロケーション"],
          "数量": r["数量"], "備考": r["備考"]} for r in period_rows],
        columns=["id", "種別", "ロケーション", "数量", "備考"])
    _qver = st.session_state.get("stk_q_ver", 0)
    type_col = (st.column_config.SelectboxColumn("種別", options=master_names)
                if master_names else st.column_config.TextColumn("種別"))
    edited = st.data_editor(
        sheet, num_rows="dynamic", use_container_width=True, hide_index=True,
        key=f"stk_q_editor_{client_name}_{ym}_{period_name}_{_qver}",
        column_config={
            "id": None,
            "種別": type_col,
            "ロケーション": st.column_config.TextColumn("ロケーション", help="例: TA, TB, ネスティング等"),
            "数量": st.column_config.NumberColumn("数量", min_value=0, step=1),
            "備考": st.column_config.TextColumn("備考", width="large"),
        })

    if st.button(f"💾 {period_name}を保存", key="stk_q_save", type="primary"):
        try:
            recs = []
            for _, e in edited.iterrows():
                e = dict(e)
                e["期"] = period_name
                e["カウント日"] = cdate.strftime("%Y/%m/%d")
                recs.append(e)
            res = notion_store.save_storage_counts(
                db_ids, client_name, recs, loaded_ids, ym)
            st.session_state.pop(f"stk_lines_{client_name}_{ym}", None)
            st.session_state["stk_q_ver"] = _qver + 1
            st.success(f"{period_name}を保存しました"
                       f"（新規{res['created']}・更新{res['updated']}・削除{res['deleted']}）。")
            st.rerun()
        except Exception as e:
            st.error(f"保存に失敗しました: {e}")

    st.markdown("---")
    st.markdown(f"#### {ym} のカウント状況（2期平均→保管料）")
    _show_summary(ym)
    st.stop()


# ============================================================
# 編集・管理モード
# ============================================================
st.subheader("編集・管理（請求担当者向け）")
st.warning("このモードは過去データの編集・削除ができます。誤操作にご注意ください。")

e1, e2 = st.columns(2)
year = e1.number_input("対象年", 2020, 2100, value=today.year, step=1, key="stk_e_year")
month = e2.selectbox("対象月", list(range(1, 13)), index=today.month - 1, key="stk_e_month")
ym = f"{int(year)}-{int(month):02d}"

if st.button("🔄 データを再読込", key="stk_e_reload"):
    st.session_state.pop(f"stk_lines_{client_name}_{ym}", None)
    st.rerun()
data_key, rows = _load(ym)

base_df = pd.DataFrame(
    [{"id": r.get("id", ""), "カウント日": r["カウント日"], "期": r["期"], "種別": r["種別"],
      "ロケーション": r["ロケーション"], "数量": r["数量"], "備考": r["備考"]} for r in rows],
    columns=["id", "カウント日", "期", "種別", "ロケーション", "数量", "備考"])
loaded_ids = [r["id"] for r in rows if r.get("id")]

with st.expander("スプレッドシートCSVから取込（移行用）", expanded=False):
    st.caption("列：カウント日 / 種別 / ロケーション(任意) / 数量 / 備考。取り込むと下の表に追加されます。")
    up = st.file_uploader("保管カウントCSV", type=["csv"], key="stk_csv")
    if up is not None:
        try:
            imported = csv_import.parse_storage_count_csv(up.getvalue())
            for r in imported:
                r["id"] = ""
                r["期"] = "第1期" if notion_store._period_from_date(r["カウント日"]) == "第1期" else "第2期"
            imp_df = pd.DataFrame(imported, columns=base_df.columns)
            base_df = pd.concat([base_df, imp_df], ignore_index=True)
            st.success(f"{len(imp_df)} 行を取り込みました（下の表で確認し『保存』してください）。")
        except Exception as e:
            st.error(f"CSV取込に失敗: {e}")

_ver = st.session_state.get("stk_editor_ver", 0)
st.markdown(f"#### {client_name}／{ym} の保管カウント明細")
edited = st.data_editor(
    base_df, num_rows="dynamic", use_container_width=True, hide_index=True,
    key=f"stk_e_editor_{client_name}_{ym}_{_ver}",
    column_config={
        "id": None,
        "カウント日": st.column_config.TextColumn("カウント日", help="例: 2026/05/15"),
        "期": st.column_config.SelectboxColumn("期", options=["第1期", "第2期"]),
        "種別": st.column_config.TextColumn("種別"),
        "ロケーション": st.column_config.TextColumn("ロケーション"),
        "数量": st.column_config.NumberColumn("数量", step=1),
        "備考": st.column_config.TextColumn("備考", width="large"),
    })

# プレビュー（種別ごと2期平均）
prev_rows = [{"種別": r.get("種別"), "期": r.get("期"), "数量": r.get("数量")}
             for _, r in edited.iterrows() if str(r.get("種別", "")).strip()]
prev, by_out, warns = notion_store.aggregate_storage(prev_rows, master_price, master_out)
if prev:
    st.dataframe(pd.DataFrame(prev), use_container_width=True, hide_index=True)
    st.metric("保管料 合計", f"{sum(by_out.values()):,} 円")
for w in warns:
    st.warning(w)


def _do_save(recs):
    try:
        res = notion_store.save_storage_counts(db_ids, client_name, recs, loaded_ids, ym)
        st.session_state.pop(data_key, None)
        st.session_state["stk_save_flash"] = (
            f"{client_name}／{ym} を保存しました"
            f"（新規{res['created']}・更新{res['updated']}・削除{res['deleted']}）。")
    except Exception as e:
        st.session_state["stk_save_flash"] = f"保存に失敗しました: {e}"


_flash = st.session_state.pop("stk_save_flash", None)
if _flash:
    st.success(_flash)

if st.button("💾 保存", key="stk_e_save", type="primary"):
    recs = edited.to_dict("records")
    _ids = {str(r.get("id")).strip() for r in recs
            if r.get("id") and str(r.get("id")).strip().lower() != "nan"}
    deletions = [i for i in loaded_ids if i not in _ids]
    if deletions:
        st.session_state["stk_pending"] = {"recs": recs, "ndel": len(deletions)}
        st.rerun()
    else:
        _do_save(recs)
        st.rerun()

_pending = st.session_state.get("stk_pending")
if _pending:
    def _reset_editor():
        st.session_state["stk_editor_ver"] = st.session_state.get("stk_editor_ver", 0) + 1

    def _render_confirm():
        st.warning(f"⚠️ {_pending['ndel']} 行を削除します。この操作は取り消せません。よろしいですか？")
        b1, b2 = st.columns(2)
        if b1.button("✅ 削除して保存", key="stk_confirm_yes", type="primary"):
            _do_save(_pending["recs"])
            st.session_state.pop("stk_pending", None)
            _reset_editor()
            st.rerun()
        if b2.button("↩️ やめる（戻る）", key="stk_confirm_no"):
            st.session_state.pop("stk_pending", None)
            _reset_editor()
            st.rerun()

    _dlg = getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None)
    if _dlg:
        @_dlg("削除の確認")
        def _confirm_dialog():
            _render_confirm()
        _confirm_dialog()
    else:
        _render_confirm()
