# -*- coding: utf-8 -*-
"""NE商品マスタの自動取得に在庫の列を足す。

守りたいこと:
  - 在庫の項目が使えなくても、原価のマスタ（最低限の6列）は今までどおり取れる
  - 取れなかった在庫の項目は **列ごと出さない**（0 で埋めない。「在庫0」と取り違える）
  - 在庫の列は最低限の6列の後ろに足す（読み手は列名で読むので既存の機能は変わらない）
  - 認証切れは握りつぶさない
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.ne_api import client, master_sync  # noqa: E402

GOODS = [
    {"goods_id": "aaa", "goods_jan_code": "4900000000001", "goods_name": "商品A", "goods_cost_price": "100",
     "goods_1_item": "60", "goods_location": "A-1", "stock_quantity": "12", "stock_allocation_quantity": "2",
     "stock_free_quantity": "10", "stock_defective_quantity": "0", "stock_remaining_order_quantity": "0",
     "stock_advance_order_quantity": "0", "goods_type_id": "0", "goods_type_name": "通常",
     "goods_merchandise_id": "0", "goods_merchandise_name": "取扱中", "goods_supplier_id": "1"},
    {"goods_id": "bbb", "goods_jan_code": "", "goods_name": "商品B", "goods_cost_price": "200",
     "goods_1_item": "80", "goods_location": "", "stock_quantity": "0", "stock_allocation_quantity": "0",
     "stock_free_quantity": "0", "stock_defective_quantity": "0", "stock_remaining_order_quantity": "5",
     "stock_advance_order_quantity": "0", "goods_type_id": "20", "goods_type_name": "受注発注",
     "goods_merchandise_id": "0", "goods_merchandise_name": "取扱中", "goods_supplier_id": "2"},
]
# セット商品マスタ（1行＝構成品1つ）
SETS = [
    {"set_goods_id": "set-1", "set_goods_name": "セット1", "set_goods_detail_goods_id": "aaa",
     "set_goods_detail_quantity": "1", "set_goods_representation_id": "aaa"},
    {"set_goods_id": "set-1", "set_goods_name": "セット1", "set_goods_detail_goods_id": "bbb",
     "set_goods_detail_quantity": "2", "set_goods_representation_id": "aaa"},
    {"set_goods_id": "set-2", "set_goods_name": "セット2", "set_goods_detail_goods_id": "bbb",
     "set_goods_detail_quantity": "1", "set_goods_representation_id": "bbb"},
]


@pytest.fixture
def ne(monkeypatch):
    """使えない項目を決めて client.call を差し替える。呼び出しを記録する。"""
    state = {"dead": set(), "calls": [], "name_in_error": True}

    def fake_call(endpoint, params=None, **kw):
        params = params or {}
        state["calls"].append((endpoint, dict(params)))
        if endpoint.endswith("/count"):
            return {"result": "success", "count": str(len(GOODS))}
        fields = [f for f in params.get("fields", "").split(",") if f]
        bad = [f for f in fields if f in state["dead"]]
        if bad:
            detail = ",".join(bad) if state["name_in_error"] else "指定された項目が不正です"
            raise client.NEError(f"NE APIエラー（{endpoint}）: 004003 {detail}")
        offset = int(params.get("offset", "0"))
        source = SETS if "setgoods" in endpoint else GOODS
        rows = source[offset:offset + int(params.get("limit", "1"))]
        return {"result": "success", "data": [{f: r.get(f, "") for f in fields} for r in rows]}

    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(master_sync.client, "call", fake_call)
    return state


def test_stock_columns_follow_the_six_basic_columns(ne):
    df, jan_ok = master_sync.fetch_master()
    assert list(df.columns) == master_sync.CANONICAL_ORDER + master_sync.STOCK_ORDER + master_sync.KUBUN_ORDER
    assert jan_ok
    assert df.loc[df["商品コード"] == "aaa", "在庫数"].iloc[0] == "12"
    assert df.loc[df["商品コード"] == "bbb", "発注残数"].iloc[0] == "5"
    assert master_sync.missing_stock_columns(df) == []


def test_checking_stock_fields_costs_one_extra_call(ne):
    master_sync.fetch_master()
    searches = [p for e, p in ne["calls"] if e.endswith("/search")]
    assert len(searches) == 2                    # 項目の確認1回 + 全件取得1回
    assert searches[0]["limit"] == "1"


def test_unavailable_stock_field_is_left_out_not_zero_filled(ne):
    ne["dead"] = {"stock_advance_order_quantity"}
    df, _ = master_sync.fetch_master()
    assert "予約在庫数" not in df.columns
    assert "在庫数" in df.columns
    assert master_sync.missing_stock_columns(df) == ["予約在庫数"]


def test_basic_master_survives_when_no_stock_field_works(ne):
    ne["dead"] = set(master_sync.STOCK_FIELD_MAP)
    df, jan_ok = master_sync.fetch_master()
    assert list(df.columns) == master_sync.CANONICAL_ORDER + master_sync.KUBUN_ORDER
    assert len(df) == len(GOODS) and jan_ok
    assert master_sync.STOCK_KEY_COLUMN in master_sync.missing_stock_columns(df)


def test_fields_are_checked_one_by_one_when_the_error_does_not_name_them(ne):
    ne["dead"] = {"stock_defective_quantity"}
    ne["name_in_error"] = False
    alive, dead = master_sync.check_stock_fields()
    assert dead == ["stock_defective_quantity"]
    assert "stock_quantity" in alive


def test_kubun_columns_come_after_stock_without_extra_calls(ne):
    df, _ = master_sync.fetch_master()
    assert df.loc[df["商品コード"] == "bbb", "商品区分"].iloc[0] == "受注発注"
    assert df.loc[df["商品コード"] == "aaa", "取扱区分"].iloc[0] == "取扱中"
    assert master_sync.missing_kubun_columns(df) == []
    searches = [p for e, p in ne["calls"] if e.endswith("/search")]
    assert len(searches) == 2                    # 区分を足しても「確認1回＋全件1回」のまま


def test_unavailable_kubun_field_is_left_out(ne):
    ne["dead"] = {"goods_supplier_id"}
    df, _ = master_sync.fetch_master()
    assert "仕入先コード" not in df.columns
    assert "商品区分" in df.columns and "在庫数" in df.columns
    assert master_sync.missing_kubun_columns(df) == ["仕入先コード"]


def test_setgoods_one_row_per_component(ne):
    df = master_sync.fetch_setgoods()
    assert list(df.columns) == master_sync.SETGOODS_ORDER
    assert len(df) == 3 and df["セット商品コード"].nunique() == 2
    parts = df[df["セット商品コード"] == "set-1"].set_index("構成商品コード")["構成数量"].to_dict()
    assert parts == {"aaa": "1", "bbb": "2"}


def test_setgoods_pages_until_a_short_page(ne, monkeypatch):
    monkeypatch.setattr(master_sync, "PAGE_LIMIT", 2)
    df = master_sync.fetch_setgoods()
    assert len(df) == 3
    pages = [p for e, p in ne["calls"] if "setgoods" in e]
    assert [p["offset"] for p in pages] == ["0", "2"]


def test_setgoods_zero_rows_is_an_empty_frame_with_columns(ne, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "SETS", [])
    df = master_sync.fetch_setgoods()
    assert df.empty and list(df.columns) == master_sync.SETGOODS_ORDER


def test_auth_error_is_not_swallowed(monkeypatch):
    def boom(endpoint, params=None, **kw):
        raise client.NEAuthError("未認可です")

    monkeypatch.setattr(master_sync.client, "call", boom)
    with pytest.raises(client.NEAuthError):
        master_sync.check_stock_fields()
