# -*- coding: utf-8 -*-
"""資材ページ共通の初期化(Notion DB準備)。"""
from . import notion_material as N


def init_material():
    """
    Notion DB(資材_*)を準備し db_ids を返す。session_stateにキャッシュ。
    呼び出し側でst.stop()するため、未設定/失敗時は例外を送出。
    """
    import streamlit as st
    if not st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", ""):
        raise RuntimeError(
            "Secrets に INVOICE_NOTION_PARENT_PAGE_ID が未設定です。"
            "請求書発行機能と同じ親ページIDを設定してください。")
    cached = st.session_state.get("material_db_ids")
    same_ver = st.session_state.get("material_schema_ver") == N.SCHEMA_VERSION
    if cached and same_ver and all(k in cached for k in N.DB_SCHEMAS):
        return cached
    # 初回 or スキーマ版数変更時は ensure_databases で不足列を自動追加(同期)
    db_ids = N.ensure_databases()
    st.session_state["material_schema_ver"] = N.SCHEMA_VERSION
    st.session_state["material_db_ids"] = db_ids
    return db_ids


def load_supplier_options():
    """
    買掛の取引先マスタ(支払_取引先マスタ)から仕入先候補を返す。
    戻り値: [{"NE仕入先cd":..., "会社名":...}] (NE仕入先cd昇順)
    取得失敗時は空リスト(資材マスタ側で手入力できるため落とさない)。
    """
    try:
        from lib.payable import app_init as PA, notion_payable as PN
        db_ids = PA.init_payable(seed_master=False)
        rows = PN.load_master(db_ids)
    except Exception:  # noqa: BLE001 - 買掛未設定でも資材機能は使える
        return []
    out = []
    for r in rows:
        out.append({"NE仕入先cd": (r.get("NE仕入先cd") or "").strip(),
                    "会社名": (r.get("会社名") or "").strip()})
    out.sort(key=lambda x: (x["NE仕入先cd"], x["会社名"]))
    return out
