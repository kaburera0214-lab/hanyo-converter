# -*- coding: utf-8 -*-
"""Yahoo updateItemsの部分回復テスト（本番APIは呼ばない）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.yahoo_api import client, items  # noqa: E402


NOT_FOUND_XML = """<ResultSet>
  <Status>NG</Status>
  <Result>
    <ErrorKey>item2</ErrorKey>
    <ItemCode>-</ItemCode>
    <Error>
      <Target>item_code</Target>
      <Code>it-02002</Code>
      <Message>指定された商品は存在しません。</Message>
    </Error>
  </Result>
</ResultSet>"""


def test_update_prices_checked_omits_missing_and_retries(monkeypatch):
    """一括更新を止めた未登録商品だけを除外し、残りを再送する。"""
    calls = []

    def _post(path, data):
        calls.append((path, dict(data)))
        if len(calls) == 1:
            raise items.YahooHTTPError(400, NOT_FOUND_XML)
        return "<ResultSet><Status>OK</Status></ResultSet>"

    monkeypatch.setattr(client, "seller_id", lambda: "test-store")
    monkeypatch.setattr(items, "_post", _post)

    ok, errors, missing = items.update_prices_checked(
        {"good001": 1000, "missing001": 2000, "good002": 3000})

    assert (ok, errors, missing) == (2, [], ["missing001"])
    assert len(calls) == 2
    assert "item_code=missing001" in calls[0][1]["item2"]
    assert list(k for k in calls[1][1] if k.startswith("item")) == ["item1", "item2"]
    assert "item_code=good001" in calls[1][1]["item1"]
    assert "item_code=good002" in calls[1][1]["item2"]


def test_update_prices_checked_does_not_hide_other_errors(monkeypatch):
    """it-02002以外は除外・再送せず、Yahooエラー本文を呼び出し側へ返す。"""
    xml = NOT_FOUND_XML.replace("it-02002", "it-02022").replace(
        "指定された商品は存在しません。", "sale_priceが指定されていません。")
    calls = []

    def _post(path, data):
        calls.append((path, dict(data)))
        raise items.YahooHTTPError(400, xml)

    monkeypatch.setattr(client, "seller_id", lambda: "test-store")
    monkeypatch.setattr(items, "_post", _post)

    ok, errors, missing = items.update_prices_checked({"good001": 1000, "good002": 2000})

    assert ok == 0 and missing == [] and len(errors) == 1
    assert "it-02022" in errors[0]
    assert len(calls) == 1
