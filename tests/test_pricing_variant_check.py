# -*- coding: utf-8 -*-
"""同じ楽天ページで改定されなかったSKUの洗い出し（lib/pricing/variant_check）。

題材は 2026-10-26 #1295 のトーマスセット。単品SKUだけが改定され、
同じページのセットSKUが旧価格のまま残った。
"""
from lib.pricing import variant_check as vc


def _v(renkei, label, price, hidden=False):
    return {"merchantDefinedSkuId": renkei, "selectorValues": {"-": label},
            "standardPrice": str(price), "hidden": hidden}


PAGES = {
    "nowo0017": {"item_price": None, "variants": {
        "7546": _v("nowo0017", "単品(+0円)", 23590),
        "7534": _v("nowo0017-a", "A：アニー(+3190円)", 21170),
        "7545": _v("nowo0017-tadh", "L：大満足セット(+26780円)", 44760),
    }},
    # 対象SKUが1つも無いページは見ない
    "nowo0005": {"item_price": None, "variants": {
        "nowo0005-bl": _v("nowo0005-bl", "ブルー", 9083),
    }},
}
REVISED = {"nowo0017": {"商品管理番号": "nowo0017", "SKU管理番号": "7546",
                        "旧価格": 23590, "新価格": 26180}}
SETS = {
    "nowo0017-a": {"parts": {"nowo0017": 1, "nowo0017-7279": 1}},
    "nowo0017-tadh": {"parts": {"nowo0017": 2, "nowo0017-7279": 1,
                                "nowo0017-7276": 1, "nowo0017-7277": 1}},
}


def test_leftovers_lists_only_other_skus_on_pages_with_a_target():
    rows = vc.leftovers(PAGES, {("nowo0017", "7546")})
    assert [r["SKU管理番号"] for r in rows] == ["7534", "7545"]
    assert rows[0]["連携SKU番号"] == "nowo0017-a"
    assert rows[0]["選択肢"] == "A：アニー(+3190円)"
    assert rows[0]["現販売価格"] == 21170


def test_leftovers_none_when_whole_page_is_revised():
    assert vc.leftovers(PAGES, {("nowo0017", "7546"), ("nowo0017", "7534"),
                                ("NOWO0017", "7545")}) == []


def test_suggest_adds_price_rise_times_quantity_of_revised_part():
    rows = vc.suggest(vc.leftovers(PAGES, {("nowo0017", "7546")}), SETS, REVISED)
    a, l = rows
    assert a["提案の根拠"] == vc.BY_SET
    assert a["提案価格"] == 21170 + 2590
    assert "nowo0017×1" in a["中身の改定"]
    # 本体2台のセットは値上げ幅も2台ぶん
    assert l["提案価格"] == 44760 + 2 * 2590


def test_suggest_falls_back_to_page_ratio_when_set_is_unknown():
    rows = vc.suggest(vc.leftovers(PAGES, {("nowo0017", "7546")}), {}, REVISED)
    assert rows[0]["提案の根拠"] == vc.BY_RATIO
    assert rows[0]["中身の改定"] == "NEのセットに中身の登録なし"
    assert rows[0]["提案価格"] == round(21170 * 26180 / 23590)


def test_decide_splits_and_requires_reason_and_price():
    base = vc.suggest(vc.leftovers(PAGES, {("nowo0017", "7546")}), SETS, REVISED)
    rows = [dict(base[0], 判断=vc.SUGGESTED), dict(base[1], 判断=vc.LATER)]
    apply_rows, later, keep, problems = vc.decide(rows)
    assert [(r["SKU管理番号"], r["価格"]) for r in apply_rows] == [("7534", 23760)]
    assert [(r["SKU管理番号"], r["価格"]) for r in later] == [("7545", 49940)]
    assert keep == [] and problems == []

    rows = [dict(base[0], 判断=vc.KEEP, 据え置きの理由=""),
            dict(base[1], 判断=vc.FIXED, 指定価格=None)]
    _, _, keep, problems = vc.decide(rows)
    assert keep == [] and len(problems) == 2

    rows = [dict(base[0], 判断=vc.KEEP, 据え置きの理由="縦積みで意図して据え置き")]
    _, _, keep, problems = vc.decide(rows)
    assert keep[0]["理由"] == "縦積みで意図して据え置き" and problems == []


def test_unknown_decision_is_treated_as_later_not_dropped():
    base = vc.suggest(vc.leftovers(PAGES, {("nowo0017", "7546")}), SETS, REVISED)
    _, later, _, _ = vc.decide([dict(base[0], 判断=None)])
    assert len(later) == 1


def test_merge_puts_extra_skus_into_the_same_patch():
    tasks = [{"商品管理番号": "nowo0017", "sku_prices": {"7546": 26180}, "対象コード": ["nowo0017"]}]
    extra = vc.to_rakuten_tasks([
        {"商品管理番号": "nowo0017", "SKU管理番号": "7534", "連携SKU番号": "nowo0017-a", "価格": 30195},
        {"商品管理番号": "other01", "SKU管理番号": "1", "連携SKU番号": "", "価格": 1000},
    ])
    merged = vc.merge_rakuten_tasks(tasks, extra)
    assert merged[0]["sku_prices"] == {"7546": 26180, "7534": 30195}
    assert merged[0]["対象コード"] == ["nowo0017", "nowo0017-a"]
    assert merged[1]["商品管理番号"] == "other01"
    # 元のタスクは書き換えない
    assert tasks[0]["sku_prices"] == {"7546": 26180}
