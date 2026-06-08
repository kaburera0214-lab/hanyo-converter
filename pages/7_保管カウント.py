# -*- coding: utf-8 -*-
"""
保管カウント入力ページ（保管料の元データ）

2期制（第1期=15日／第2期=末日）の在庫数を、クライアント×対象月×種別で記録する。
請求書発行ページが、種別ごとの2期平均 × 単価 で保管料を自動算出する。

2つのモード（イレギュラー作業と同じ思想）:
  - かんたん入力（現場・既定）: 期を選び、種別ごとに数量を入れて一括保存。
    その期だけ更新し、もう一方の期は触らない。
  - 編集・管理（請求担当向け）: 種別・両期・単価・出力品名をテーブル編集。削除は確認つき。
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="保管カウント入力", layout="wide")
st.title("保管カウント入力")
st.caption("月2回（第1期=15日／第2期=末日）の在庫数を記録します。請求書の保管料に自動反映されます。")

from lib.invoice import store, notion_store


# --- Notion初期化（請求書ページと共有） ---
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


# --- クライアント・モード ---
cc1, cc2 = st.columns([2, 3])
_def_idx = client_names.index("Team-EC") if "Team-EC" in client_names else 0
client_name = cc1.selectbox("クライアント", client_names, index=_def_idx, key="stk_client")
mode = cc2.radio("モード", ["かんたん入力（カウント記録）", "編集・管理（請求担当者向け）"],
                 horizontal=True, key="stk_mode")

master = clients.get(client_name, {}).get("保管料マスタ", [])
master_price = {m["種別名"]: m["単価"] for m in master}
master_out = {m["種別名"]: m["出力品名"] for m in master}


def _load_month(ym):
    """対象月の保管内訳を {種別名: row} で返す。"""
    key = f"stk_data_{client_name}_{ym}"
    if key not in st.session_state:
        try:
            rows = notion_store.load_storage_history(db_ids, client_name, ym)
        except Exception as e:
            rows = []
            st.error(f"読込に失敗: {e}")
        st.session_state[key] = {r["種別名"]: r for r in rows}
    return key, st.session_state[key]


def _save_month(ym, rows):
    """rows(種別名/15日数量/末日数量/単価/出力品名)を平均・金額付きで保存。"""
    payload = []
    for r in rows:
        name = str(r.get("種別名", "")).strip()
        if not name:
            continue
        q1 = float(r.get("15日数量") or 0)
        q2 = float(r.get("末日数量") or 0)
        price = float(r.get("単価") or 0)
        avg = (q1 + q2) / 2
        payload.append({
            "種別名": name, "15日数量": q1, "末日数量": q2,
            "平均数量": avg, "単価": price, "金額": round(avg * price),
            "出力品名": str(r.get("出力品名", "")).strip() or "保管料"})
    notion_store.save_storage_history(
        db_ids, client_name=client_name, target_ym=ym, storage_rows=payload)
    st.session_state.pop(f"stk_data_{client_name}_{ym}", None)
    return len(payload)


def _summary(rows_dict):
    """種別dictから集計プレビューと出力品名別合計を返す。"""
    prev, by_out = [], {}
    for name, r in rows_dict.items():
        q1 = float(r.get("15日数量") or 0)
        q2 = float(r.get("末日数量") or 0)
        price = float(r.get("単価") or master_price.get(name, 0) or 0)
        out = r.get("出力品名") or master_out.get(name, "保管料")
        avg = (q1 + q2) / 2
        amt = round(avg * price)
        prev.append({"種別名": name, "第1期(15日)": q1, "第2期(末日)": q2,
                     "平均": avg, "単価": price, "金額": amt, "出力品名": out})
        by_out[out] = by_out.get(out, 0) + amt
    return prev, by_out


# ============================================================
# かんたん入力モード（期を選んで種別ごとに数量一括）
# ============================================================
if mode.startswith("かんたん"):
    s1, s2, s3 = st.columns([1, 1, 2])
    year = s1.number_input("対象年", 2020, 2100, value=today.year, step=1, key="stk_q_year")
    month = s2.selectbox("対象月", list(range(1, 13)), index=today.month - 1, key="stk_q_month")
    period = s3.radio("カウント期", ["第1期（15日）", "第2期（末日）"],
                      horizontal=True, key="stk_q_period")
    ym = f"{int(year)}-{int(month):02d}"
    qty_col = "15日数量" if period.startswith("第1") else "末日数量"

    if st.button("🔄 最新に更新", key="stk_q_reload"):
        st.session_state.pop(f"stk_data_{client_name}_{ym}", None)
        st.rerun()
    data_key, existing = _load_month(ym)

    # 種別＝マスタ ∪ 既存
    names = list(dict.fromkeys(list(master_price.keys()) + list(existing.keys())))
    if not names:
        st.info("保管料マスタに種別がありません。先に「請求書発行」→単価マスタ管理で保管の種別を登録してください。")
        st.stop()

    st.markdown(f"#### {client_name}／{ym}／{period} の数量入力")
    st.caption("各種別の数量を入れて保存してください。選んだ期だけ更新され、もう一方の期は変わりません。")
    sheet = pd.DataFrame([
        {"種別名": n, "単価": master_price.get(n, existing.get(n, {}).get("単価", 0)),
         "数量": existing.get(n, {}).get(qty_col, 0)} for n in names])
    edited = st.data_editor(
        sheet, use_container_width=True, hide_index=True, key=f"stk_q_editor_{client_name}_{ym}_{qty_col}",
        column_config={
            "種別名": st.column_config.TextColumn("種別名", disabled=True),
            "単価": st.column_config.NumberColumn("単価", disabled=True),
            "数量": st.column_config.NumberColumn("数量", min_value=0, step=1),
        })

    if st.button(f"💾 {period}を保存", key="stk_q_save", type="primary"):
        try:
            rows = []
            for _, e in edited.iterrows():
                n = e["種別名"]
                base = existing.get(n, {})
                row = {"種別名": n,
                       "15日数量": base.get("15日数量", 0),
                       "末日数量": base.get("末日数量", 0),
                       "単価": master_price.get(n, base.get("単価", 0)),
                       "出力品名": master_out.get(n, base.get("出力品名", "保管料"))}
                row[qty_col] = float(e["数量"] or 0)
                rows.append(row)
            n = _save_month(ym, rows)
            st.success(f"{period}を保存しました（{n}種別）。請求書発行ページの保管料に反映されます。")
        except Exception as e:
            st.error(f"保存に失敗しました: {e}")

    # 当月サマリ（読み取り）
    st.markdown("---")
    st.markdown(f"#### {ym} のカウント状況（2期平均→保管料）")
    _, cur = _load_month(ym)
    prev, by_out = _summary(cur)
    if prev:
        st.dataframe(pd.DataFrame(prev), use_container_width=True, hide_index=True)
        st.metric("保管料 合計", f"{sum(by_out.values()):,} 円")
    else:
        st.info("まだ数量が入っていません。")
    st.stop()


# ============================================================
# 編集・管理モード
# ============================================================
st.subheader("編集・管理（請求担当者向け）")
st.warning("このモードは種別の追加・削除や両期の数値を直接編集できます。誤操作にご注意ください。")

e1, e2 = st.columns([1, 1])
year = e1.number_input("対象年", 2020, 2100, value=today.year, step=1, key="stk_e_year")
month = e2.selectbox("対象月", list(range(1, 13)), index=today.month - 1, key="stk_e_month")
ym = f"{int(year)}-{int(month):02d}"

if st.button("🔄 データを再読込", key="stk_e_reload"):
    st.session_state.pop(f"stk_data_{client_name}_{ym}", None)
    st.rerun()
data_key, existing = _load_month(ym)

# マスタにあって未登録の種別も初期表示
names = list(dict.fromkeys(list(existing.keys()) + list(master_price.keys())))
base_df = pd.DataFrame([
    {"種別名": n,
     "15日数量": existing.get(n, {}).get("15日数量", 0),
     "末日数量": existing.get(n, {}).get("末日数量", 0),
     "単価": existing.get(n, {}).get("単価", master_price.get(n, 0)),
     "出力品名": existing.get(n, {}).get("出力品名", master_out.get(n, "保管料"))}
    for n in names],
    columns=["種別名", "15日数量", "末日数量", "単価", "出力品名"])
loaded_names = set(existing.keys())

_ver = st.session_state.get("stk_editor_ver", 0)
st.markdown(f"#### {client_name}／{ym} の保管内訳")
edited = st.data_editor(
    base_df, num_rows="dynamic", use_container_width=True, hide_index=True,
    key=f"stk_e_editor_{client_name}_{ym}_{_ver}",
    column_config={
        "種別名": st.column_config.TextColumn("種別名"),
        "15日数量": st.column_config.NumberColumn("第1期(15日)数量", min_value=0, step=1),
        "末日数量": st.column_config.NumberColumn("第2期(末日)数量", min_value=0, step=1),
        "単価": st.column_config.NumberColumn("単価", min_value=0, step=10),
        "出力品名": st.column_config.TextColumn("出力品名（MF品目名）"),
    })

# プレビュー
prev_rows = {}
for _, r in edited.iterrows():
    n = str(r.get("種別名", "")).strip()
    if not n:
        continue
    prev_rows[n] = {"15日数量": r.get("15日数量"), "末日数量": r.get("末日数量"),
                    "単価": r.get("単価"), "出力品名": r.get("出力品名")}
prev, by_out = _summary(prev_rows)
if prev:
    st.dataframe(pd.DataFrame(prev), use_container_width=True, hide_index=True)
    st.metric("保管料 合計", f"{sum(by_out.values()):,} 円")


def _do_save_edit(rows):
    try:
        n = _save_month(ym, rows)
        st.session_state["stk_save_flash"] = f"{client_name}／{ym} の保管内訳を保存しました（{n}種別）。"
    except Exception as e:
        st.session_state["stk_save_flash"] = f"保存に失敗しました: {e}"


_flash = st.session_state.pop("stk_save_flash", None)
if _flash:
    st.success(_flash)

if st.button("💾 保存", key="stk_e_save", type="primary"):
    edited_names = {str(r.get("種別名", "")).strip()
                   for _, r in edited.iterrows() if str(r.get("種別名", "")).strip()}
    removed = [n for n in loaded_names if n not in edited_names]
    rows = [dict(種別名=n, **v) for n, v in prev_rows.items()]
    if removed:
        st.session_state["stk_pending"] = {"rows": rows, "removed": removed}
        st.rerun()
    else:
        _do_save_edit(rows)
        st.rerun()

_pending = st.session_state.get("stk_pending")
if _pending:
    def _reset_editor():
        st.session_state["stk_editor_ver"] = st.session_state.get("stk_editor_ver", 0) + 1

    def _render_confirm():
        st.warning(f"⚠️ {len(_pending['removed'])} 種別を削除します（{'、'.join(_pending['removed'])}）。"
                   "この操作は取り消せません。よろしいですか？")
        b1, b2 = st.columns(2)
        if b1.button("✅ 削除して保存", key="stk_confirm_yes", type="primary"):
            _do_save_edit(_pending["rows"])
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
