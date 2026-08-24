# -*- coding: utf-8 -*-
"""入荷登録runnerのYahoo価格更新テスト（本番APIは呼ばない）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lib.yahoo_api as yahoo_api  # noqa: E402
from lib.receiving import runner  # noqa: E402


def test_yahoo_missing_item_is_skipped_and_valid_item_is_published(monkeypatch):
    calls = {"prices": [], "publish": 0}

    class _Items:
        @staticmethod
        def update_prices_checked(price_by_code):
            calls["prices"].append(price_by_code)
            return 1, [], ["missing001"]

        @staticmethod
        def reserve_publish():
            calls["publish"] += 1
            return []

    class _Client:
        @staticmethod
        def access_token():
            return "dummy"

    monkeypatch.setattr(yahoo_api, "items", _Items, raising=False)
    monkeypatch.setattr(yahoo_api, "client", _Client, raising=False)
    monkeypatch.setitem(sys.modules, "lib.yahoo_api.items", _Items)
    monkeypatch.setitem(sys.modules, "lib.yahoo_api.client", _Client)

    results, failed = runner.execute(
        {"yahoo_price": {"good001": 1000, "missing001": 2000}})

    assert failed == {}
    assert [r["状態"] for r in results] == ["スキップ", "成功"]
    assert "missing001" in results[0]["対象"]
    assert "it-02002" in results[0]["メッセージ"]
    assert calls["prices"] == [{"good001": 1000, "missing001": 2000}]
    assert calls["publish"] == 1


def test_yahoo_error_remains_retryable(monkeypatch):
    class _Items:
        @staticmethod
        def update_prices_checked(price_by_code):
            return 0, ["Yahoo APIエラー HTTP 503: maintenance"], []

        @staticmethod
        def reserve_publish():
            raise AssertionError("価格更新失敗時は反映予約しない")

    class _Client:
        @staticmethod
        def access_token():
            return "dummy"

    monkeypatch.setattr(yahoo_api, "items", _Items, raising=False)
    monkeypatch.setattr(yahoo_api, "client", _Client, raising=False)
    monkeypatch.setitem(sys.modules, "lib.yahoo_api.items", _Items)
    monkeypatch.setitem(sys.modules, "lib.yahoo_api.client", _Client)

    tasks = {"yahoo_price": {"good001": 1000}}
    results, failed = runner.execute(tasks)

    assert results[0]["状態"] == "失敗"
    assert "HTTP 503" in results[0]["メッセージ"]
    assert failed == tasks
