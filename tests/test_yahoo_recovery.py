# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.pricing import yahoo_recovery as yr  # noqa: E402


def _queue():
    return pd.DataFrame([
        {"code": "good001", "price": 900},
        {"code": "diff001", "price": 1000},
        {"code": "missing001", "price": 1100},
    ])


def test_fetch_rakuten_parent_price_requires_uniform_sku_price(monkeypatch):
    monkeypatch.setattr(yr.rms_api, "get", lambda path: {"item": {"variants": {
        "sku1": {"standardPrice": "1200"}, "sku2": {"standardPrice": 1200}}}})
    assert yr.fetch_rakuten_parent_price("good001") == (1200, "")

    monkeypatch.setattr(yr.rms_api, "get", lambda path: {"item": {"variants": {
        "sku1": {"standardPrice": 1200}, "sku2": {"standardPrice": 1300}}}})
    price, message = yr.fetch_rakuten_parent_price("diff001")
    assert price is None and "SKU間で価格が異なる" in message


def test_process_next_updates_only_safe_prices(monkeypatch):
    state = yr.new_state(_queue())

    def _fetch(code):
        if code == "good001":
            return 1200, ""
        if code == "diff001":
            return None, "楽天のSKU間で価格が異なるため自動反映対象外"
        return 1400, ""

    monkeypatch.setattr(yr, "fetch_rakuten_parent_price", _fetch)
    monkeypatch.setattr(yr.yahoo_client, "access_token", lambda: "token")
    monkeypatch.setattr(yr.yahoo_items, "update_prices_checked",
                        lambda prices: (1, [], ["missing001"]))
    published = []
    monkeypatch.setattr(yr.yahoo_items, "reserve_publish",
                        lambda: published.append(True) or [])

    yr.process_next(_queue(), state)

    results = state["results"]
    assert results["good001"]["status"] == "Yahoo反映成功"
    assert results["good001"]["rakuten_price"] == 1200
    assert results["diff001"]["status"] == "楽天対象外"
    assert results["missing001"]["status"] == "Yahoo商品なし"
    assert published == [True]
    assert yr.summary(_queue(), state)["remaining"] == 0


def test_yahoo_failure_is_recorded_without_publish(monkeypatch):
    queue = pd.DataFrame([{"code": "good001", "price": 900}])
    state = yr.new_state(queue)
    monkeypatch.setattr(yr, "fetch_rakuten_parent_price", lambda code: (1200, ""))
    monkeypatch.setattr(yr.yahoo_client, "access_token", lambda: "token")
    monkeypatch.setattr(yr.yahoo_items, "update_prices_checked",
                        lambda prices: (0, ["HTTP 503"], []))
    monkeypatch.setattr(yr.yahoo_items, "reserve_publish",
                        lambda: (_ for _ in ()).throw(AssertionError("must not publish")))

    yr.process_next(queue, state)

    assert state["results"]["good001"]["status"] == "Yahoo更新失敗"
    assert "HTTP 503" in state["results"]["good001"]["message"]
    assert yr.reset_retryable(state) == 1
    assert yr.remaining_codes(queue, state) == ["good001"]
