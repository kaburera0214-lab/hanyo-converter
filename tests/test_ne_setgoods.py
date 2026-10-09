# -*- coding: utf-8 -*-
"""NEセット商品マスタの登録（lib/ne_api/setgoods）。

守りたいこと:
  - **すでにあるセット商品コードは送らない**（送ったときに上書きか追加かが公式に書かれていない）
  - 確認だけ（apply=False）のときは、NEへ何も送らない
  - 内訳の商品がNEに1つでも無いセットは送らない（内訳の欠けたセットを作らない）
  - 送ったあと引き直して、内訳・数量・価格・代表商品コードが計画どおりかを見る。違えば失敗
  - 計画CSVに少しでもおかしな所があれば、何も送らずに止まる
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ne_api import client, goods, setgoods  # noqa: E402

HEAD = "セット商品コード,セット商品名,セット商品販売価格,商品コード,数量,代表商品コード"
PLAN = "\n".join([
    HEAD,
    "set-a-01,セットA【はやぶさ】,3880,train-01,1,page-x",
    "set-a-01,セットA【はやぶさ】,3880,socks-01,1,page-x",
    "set-a-01,セットA【はやぶさ】,3880,bag-s,1,page-x",
    "set-b-01,セットB【はやぶさ】,2180,glove-01,1,page-x",
    "set-b-01,セットB【はやぶさ】,2180,socks-01,2,page-x",
])
ALL_GOODS = {"train-01", "socks-01", "bag-s", "glove-01"}


@pytest.fixture
def ne(monkeypatch):
    """NEの中身（商品・セット）を持つ偽物。upload は受け取ったCSVを中身に反映する。"""
    state = {"goods": set(ALL_GOODS), "sets": {}, "calls": [], "uploads": [],
             "que_status": 2, "keep_daihyo": True}

    def fake_call(endpoint, params=None, **kw):
        p = params or {}
        state["calls"].append((endpoint, dict(p)))
        if endpoint == "api_v1_master_goods/search":
            wanted = {c.strip().lower() for c in p["goods_id-in"].split(",")}
            return {"result": "success", "data": [{"goods_id": g} for g in sorted(state["goods"]) if g in wanted]}
        if endpoint == "api_v1_master_setgoods/search":
            wanted = {c.strip().lower() for c in p["set_goods_id-in"].split(",")}
            rows = []
            for code, e in state["sets"].items():
                if code.lower() in wanted:
                    rows += [{"set_goods_id": code, "set_goods_name": e["name"], "set_goods_selling_price": str(e["price"]),
                              "set_goods_detail_goods_id": g, "set_goods_detail_quantity": str(q),
                              "set_goods_representation_id": e["daihyo"]} for g, q in e["parts"]]
            return {"result": "success", "data": rows}
        if endpoint == "api_v1_master_setgoods/upload":
            state["uploads"].append(p["data"])
            lines = [line.split(",") for line in p["data"].strip().split("\r\n")]
            head = lines[0]
            for values in lines[1:]:
                row = dict(zip(head, values))
                e = state["sets"].setdefault(row["set_syohin_code"], {
                    "name": row["set_syohin_name"], "price": int(row["set_baika_tnk"]),
                    "daihyo": row.get("daihyo_syohin_code", "") if state["keep_daihyo"] else "", "parts": []})
                e["parts"].append((row["syohin_code"], int(row["suryo"])))
            return {"result": "success", "que_id": "77"}
        if endpoint == "api_v1_system_que/search":
            return {"result": "success", "data": [{"que_id": "77", "que_status_id": str(state["que_status"]),
                                                   "que_message": "NE側のエラー文" if state["que_status"] == -1 else ""}]}
        raise AssertionError(f"想定していない呼び出し: {endpoint}")

    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(goods.time, "sleep", lambda s: None)
    return state


# ---- 計画CSV ---------------------------------------------------------------

def test_plan_csv_is_grouped_by_set_code():
    sets = setgoods.parse_plan(PLAN)
    assert list(sets) == ["set-a-01", "set-b-01"]
    assert sets["set-a-01"]["parts"] == [("train-01", 1), ("socks-01", 1), ("bag-s", 1)]
    assert sets["set-b-01"] == {"name": "セットB【はやぶさ】", "price": 2180, "daihyo": "page-x",
                                "parts": [("glove-01", 1), ("socks-01", 2)]}


@pytest.mark.parametrize("bad, word", [
    ("セット商品コード,セット商品名,商品コード,数量\nx,y,z,1", "見出し"),
    (HEAD + "\nset-a,名前,100,,1,p", "空"),
    (HEAD + "\nset_a,名前,100,g,1,p", "半角英数字とハイフン"),
    (HEAD + "\n" + "s" * 31 + ",名前,100,g,1,p", "30字"),
    (HEAD + "\nset-a,名前,100,g,0,p", "1以上"),
    (HEAD + "\nset-a,名前,百,g,1,p", "整数"),
    (HEAD + "\nset-a,名前,100,g1,1,p\nset-a,名前,200,g2,1,p", "行によって違います"),
    (HEAD + "\nset-a,名前,100,g1,1,p\nset-a,名前,100,G1,1,p", "2回"),
    (HEAD + "\nset-a,名前,100,set-a,1,p", "同じです"),
    (HEAD, "1つもありません"),
])
def test_broken_plan_csv_stops_before_anything_is_sent(bad, word):
    with pytest.raises(setgoods.PlanError, match=word):
        setgoods.parse_plan(bad)


def test_only_narrows_the_plan_and_rejects_unknown_codes():
    sets = setgoods.parse_plan(PLAN)
    assert list(setgoods.pick(sets, ["set-b-01"])) == ["set-b-01"]
    assert list(setgoods.pick(sets, [])) == ["set-a-01", "set-b-01"]
    with pytest.raises(setgoods.PlanError, match="計画CSVに無い"):
        setgoods.pick(sets, ["set-zzz"])


# ---- 振り分け ---------------------------------------------------------------

def test_plan_sorts_sets_into_four_kinds():
    sets = setgoods.parse_plan(PLAN + "\nset-c-01,セットC,100,train-01,1,page-x\ntrain-01x,セットD,100,train-01,1,page-x")
    decided = setgoods.plan(sets, existing_sets={"set-a-01"},
                            existing_goods={"train-01", "socks-01", "train-01x"})
    assert decided["set-a-01"][0] == setgoods.EXISTS
    assert decided["set-b-01"] == (setgoods.MISSING_GOODS, "無い商品コード: glove-01")
    assert decided["set-c-01"] == (setgoods.NEW, "")
    assert decided["train-01x"][0] == setgoods.CODE_CONFLICT


# ---- 送るCSV ---------------------------------------------------------------

def test_upload_csv_uses_the_documented_columns_then_daihyo():
    data, used = setgoods.build_csv(setgoods.parse_plan(PLAN))
    lines = data.strip().split("\r\n")
    assert used is True
    assert lines[0] == "set_syohin_code,syohin_code,suryo,set_baika_tnk,set_syohin_name,daihyo_syohin_code"
    assert lines[1] == "set-a-01,train-01,1,3880,セットA【はやぶさ】,page-x"
    assert len(lines) == 6


def test_daihyo_column_is_left_out_when_asked_or_when_empty():
    sets = setgoods.parse_plan(PLAN)
    data, used = setgoods.build_csv(sets, with_daihyo=False)
    assert used is False and "daihyo" not in data
    empty = setgoods.parse_plan(PLAN.replace(",page-x", ","))
    data, used = setgoods.build_csv(empty)
    assert used is False and data.splitlines()[0] == "set_syohin_code,syohin_code,suryo,set_baika_tnk,set_syohin_name"


def test_mixed_daihyo_is_rejected():
    sets = setgoods.parse_plan(PLAN)
    sets["set-b-01"]["daihyo"] = ""
    with pytest.raises(setgoods.PlanError, match="混ざって"):
        setgoods.build_csv(sets)


# ---- 実行 -------------------------------------------------------------------

def test_dry_run_sends_nothing(ne):
    out = setgoods.execute(setgoods.parse_plan(PLAN), apply=False)
    assert ne["uploads"] == []
    assert out["sent"] == 0 and out["failed"] is False
    assert out["rows"]["set-a-01"][1:3] == [setgoods.NEW, "確認だけ（送っていない）"]


def test_apply_registers_new_sets_and_checks_them(ne):
    out = setgoods.execute(setgoods.parse_plan(PLAN), apply=True)
    assert len(ne["uploads"]) == 1
    assert out["sent"] == 2 and out["used_daihyo"] is True and out["checked"] == 2
    assert out["mismatched"] == 0 and out["failed"] is False
    assert out["rows"]["set-b-01"][2] == "NEの処理が完了／中身は計画どおり"


def test_existing_set_is_never_sent_again(ne):
    setgoods.execute(setgoods.parse_plan(PLAN), apply=True)
    ne["uploads"].clear()
    out = setgoods.execute(setgoods.parse_plan(PLAN), apply=True)      # 同じ計画をもう一度
    assert ne["uploads"] == []                                          # 2回目は何も送らない
    assert out["counts"] == {setgoods.EXISTS: 2} and out["failed"] is False
    assert out["rows"]["set-a-01"][2] == "中身は計画どおり"


def test_only_new_sets_go_into_the_upload(ne):
    ne["sets"]["set-a-01"] = {"name": "x", "price": 3880, "daihyo": "page-x",
                              "parts": [("train-01", 1), ("socks-01", 1), ("bag-s", 1)]}
    setgoods.execute(setgoods.parse_plan(PLAN), apply=True)
    assert "set-a-01" not in ne["uploads"][0] and "set-b-01" in ne["uploads"][0]


def test_existing_set_that_differs_from_the_plan_is_reported_not_fixed(ne):
    ne["sets"]["set-a-01"] = {"name": "x", "price": 3880, "daihyo": "page-x",
                              "parts": [("train-01", 1), ("socks-01", 2)]}       # 数量が違う・袋が無い
    out = setgoods.execute(setgoods.pick(setgoods.parse_plan(PLAN), ["set-a-01"]), apply=True)
    assert ne["uploads"] == []
    assert out["failed"] is True and out["mismatched"] == 1
    assert "内訳に無い: bag-s" in out["rows"]["set-a-01"][3] and "数量が違う: socks-01" in out["rows"]["set-a-01"][3]


def test_set_with_a_missing_component_is_not_sent(ne):
    ne["goods"].discard("glove-01")
    out = setgoods.execute(setgoods.parse_plan(PLAN), apply=True)
    assert "set-b-01" not in ne["uploads"][0]
    assert out["blocked"] == 1 and out["failed"] is True
    assert out["rows"]["set-b-01"][1:3] == [setgoods.MISSING_GOODS, "登録できない"]
    assert out["rows"]["set-a-01"][2] == "NEの処理が完了／中身は計画どおり"     # 問題の無いセットは登録する


def test_failed_queue_is_a_failure_with_the_ne_message(ne):
    ne["que_status"] = -1
    out = setgoods.execute(setgoods.parse_plan(PLAN), apply=True)
    assert out["que_ok"] is False and out["failed"] is True and out["checked"] == 0
    assert out["rows"]["set-a-01"][2:] == ["NEの処理が失敗", "NE側のエラー文"]


def test_daihyo_that_did_not_land_is_caught_by_the_recheck(ne):
    """代表商品コードの列は公式の説明に無い。送っても入らなかったら、黙って成功にしない。"""
    ne["keep_daihyo"] = False
    out = setgoods.execute(setgoods.parse_plan(PLAN), apply=True)
    assert out["failed"] is True and out["mismatched"] == 2
    assert "代表商品コードが違う" in out["rows"]["set-a-01"][3]


def test_without_daihyo_the_recheck_does_not_look_at_it(ne):
    ne["keep_daihyo"] = False
    out = setgoods.execute(setgoods.parse_plan(PLAN), apply=True, with_daihyo=False)
    assert out["used_daihyo"] is False and out["failed"] is False


def test_api_calls_stay_small(ne):
    """NE APIは呼び出し回数で課金される。20セットでも 引く2回＋送る1回＋待つ1回＋引き直す1回。"""
    setgoods.execute(setgoods.parse_plan(PLAN), apply=True)
    assert [e for e, _ in ne["calls"]] == [
        "api_v1_master_setgoods/search", "api_v1_master_goods/search",
        "api_v1_master_setgoods/upload", "api_v1_system_que/search", "api_v1_master_setgoods/search"]
