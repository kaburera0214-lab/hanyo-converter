# -*- coding: utf-8 -*-
"""買掛ページ共通の初期化(Notion DB準備＋マスタseed)。"""
import csv
import os

from . import notion_payable as N

SEED_CSV = os.path.join(os.path.dirname(__file__), "..", "..", "payable_master_seed.csv")


def load_seed_rows():
    """payable_master_seed.csv を辞書リストで返す(無ければ空)。"""
    path = os.path.abspath(SEED_CSV)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def init_payable(seed_master=True):
    """
    Notion DB(支払_*)を準備し db_ids を返す。session_stateにキャッシュ。
    seed_master=Trueなら、マスタが空のときだけseed CSVを投入する。
    呼び出し側でst.stop()するため、未設定/失敗時は例外を送出。
    """
    import streamlit as st
    if not st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", ""):
        raise RuntimeError(
            "Secrets に INVOICE_NOTION_PARENT_PAGE_ID が未設定です。"
            "請求書発行機能と同じ親ページIDを設定してください。")
    cached = st.session_state.get("payable_db_ids")
    if cached and all(k in cached for k in N.DB_SCHEMAS):
        return cached
    db_ids = N.ensure_databases()
    if seed_master:
        try:
            seeded = N.seed_master_if_empty(db_ids, load_seed_rows())
            if seeded:
                st.toast(f"取引先マスタを初期投入しました（{seeded}社）")
        except Exception as e:  # noqa: BLE001
            st.warning(f"マスタ初期投入をスキップ: {e}")
    st.session_state["payable_db_ids"] = db_ids
    return db_ids
