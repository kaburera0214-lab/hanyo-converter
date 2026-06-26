# -*- coding: utf-8 -*-
"""資材ページ共通の初期化(Notion DB準備＋マスタseed)。"""
import csv
import os

from . import notion_material as N

SEED_CSV = os.path.join(os.path.dirname(__file__), "..", "..", "material_master_seed.csv")


def load_seed_rows():
    """material_master_seed.csv を辞書リストで返す(無ければ空)。"""
    path = os.path.abspath(SEED_CSV)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as fp:
        return list(csv.DictReader(fp))


def init_material(seed_master=True):
    """
    Notion DB(資材_*)を準備し db_ids を返す。session_stateにキャッシュ。
    seed_master=Trueなら、未登録の資材名だけを seed CSV から投入する(冪等)。
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
    # seedはセッション中1回だけ・資材名集合で判定する冪等版(二重投入を防ぐ)
    if seed_master and not st.session_state.get("material_seeded"):
        try:
            seeded = N.seed_master_missing(db_ids, load_seed_rows())
            if seeded:
                st.toast(f"資材マスタに{seeded}件を投入しました")
            st.session_state["material_seeded"] = True
        except Exception as e:  # noqa: BLE001
            st.warning(f"マスタ初期投入をスキップ: {e}")
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
