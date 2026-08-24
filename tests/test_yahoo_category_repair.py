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

    plan = cr.infer_product_category("artc0001")

    assert plan["category_id"] == 38099
    assert plan["source"] == "JAN"
    assert "2/3" in plan["reason"]


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
