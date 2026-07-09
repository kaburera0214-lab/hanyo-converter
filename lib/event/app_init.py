# -*- coding: utf-8 -*-
"""イベントLPページ共通の初期化(Notion DB準備)。payable/app_initと同型。"""
from . import notion_event as N


def init_event():
    """
    Notion DB(イベント_*)を準備し db_ids を返す。session_stateにキャッシュ。
    呼び出し側でst.stop()するため、未設定/失敗時は例外を送出。
    """
    import streamlit as st
    if not st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", ""):
        raise RuntimeError(
            "Secrets に INVOICE_NOTION_PARENT_PAGE_ID が未設定です。"
            "請求書発行機能と同じ親ページIDを設定してください。")
    cached = st.session_state.get("event_db_ids")
    same_ver = st.session_state.get("event_schema_ver") == N.SCHEMA_VERSION
    if cached and same_ver and all(k in cached for k in N.DB_SCHEMAS):
        return cached
    db_ids = N.ensure_databases()
    st.session_state["event_schema_ver"] = N.SCHEMA_VERSION
    st.session_state["event_db_ids"] = db_ids
    return db_ids
