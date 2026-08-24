# -*- coding: utf-8 -*-
from lib.yahoo_api import category_repair as cr


def test_infer_category_prefers_same_jan_majority(monkeypatch):
    monkeypatch.setattr(cr, "get_item", lambda code: {
        "code": code, "name": "木製ハンマー", "jan": "4900000000001", "product_category": ""})
    monkeypatch.setattr(cr, "_search_hits", lambda item: ("JAN", [
        {"name": "木製ハンマー A", "janCode": item["jan"],
         "genreCategory": {"id": 38099, "name": "ハンマー"}},
        {"name": "木製ハンマー B", "janCode": item["jan"],
         "genreCategory": {"id": 38099, "name": "ハンマー"}},
        {"name": "木製ハンマー C", "janCode": item["jan"],
         "genreCategory": {"id": 12345, "name": "工具"}},
    ]))
    monkeypatch.setattr(cr, "_nearest_store_category", lambda item: None)

    plan = cr.infer_product_category("artc0001")

    assert plan["category_id"] == 38099
    assert plan["source"] == "JAN"
    assert "2/3" in plan["reason"]


def test_infer_category_prefers_similar_neighbor_in_same_store(monkeypatch):
    target = {"code": "artc3132", "name": "しんちゅう釘 32mm40本組",
              "jan": "4521718453095", "product_category": ""}
    candidate = {**target, "category_id": 34649, "category_name": "",
                 "source": "Yahoo店内類似商品", "candidate_name": "しんちゅうメッキ釘 25mm",
                 "score": 0.8, "reason": "店内類似商品 artc3131"}
    monkeypatch.setattr(cr, "get_item", lambda code: target)
    monkeypatch.setattr(cr, "_nearest_store_category", lambda item: candidate)
    monkeypatch.setattr(cr, "_search_hits",
                        lambda item: (_ for _ in ()).throw(AssertionError("public search not needed")))

    plan = cr.infer_product_category("artc3132")

    assert plan["category_id"] == 34649
    assert plan["source"] == "Yahoo店内類似商品"


def test_category_price_csv_is_partial_update_format():
    plans = {"artc0001": {"category_id": 38099}}
    text = cr._category_price_csv(plans, {"artc0001": 1000}).decode("cp932")

    assert text.splitlines()[0] == "code,product-category,price,sale-price,member-price"
    assert text.splitlines()[1] == "artc0001,38099,1000,,980"
    assert "name" not in text and "path" not in text


def test_repair_uses_saved_plan_without_reinferring(monkeypatch):
    plan = {"code": "artc0001", "category_id": 38099, "category_name": "ハンマー"}
    uploaded = {}
    monkeypatch.setattr(cr, "plan_categories",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not infer")))
    monkeypatch.setattr(cr, "upload_category_prices",
                        lambda plans, prices: uploaded.update(plans) or [])

    repaired, failures = cr.repair_category_prices(
        {"artc0001": 1000}, plans={"artc0001": plan})

    assert failures == {}
    assert repaired == {"artc0001": plan}
    assert uploaded == repaired
