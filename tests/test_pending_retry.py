# -*- coding: utf-8 -*-
"""
反映できなかった処理の控え（lib/pending_retry.py）と、その滞留監視のテスト。

押さえたいこと:
  1. 失敗分は控えられ、再実行で通った分だけ減り、全部通ったら済みになる
  2. 何も消さない（最初の失敗分・再実行の履歴・取り下げの理由が残る）
  3. 読めないときに「0件」にしない
  4. 残ったまま1日経ったら監視が赤になる
"""
import datetime
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import pending_retry as pr  # noqa: E402

UTC = datetime.timezone.utc


@pytest.fixture
def store(monkeypatch):
    """Driveの代わりにメモリへ読み書きする。"""
    box = {"entries": []}
    monkeypatch.setattr(pr, "_read_all", lambda: json.loads(json.dumps(box["entries"])))
    monkeypatch.setattr(pr, "_write_all",
                        lambda entries: box.__setitem__("entries", json.loads(
                            json.dumps(entries, ensure_ascii=False, default=str))))
    return box


def test_失敗分を控えて再実行で減り全部通ると済みになる(store):
    rid = pr.add("receiving", {"ne_price": [], "yahoo_price": {"a01": 1708, "b02": 900}},
                 "20261007_001_入荷登録")
    assert rid
    (entry,) = pr.open_of(store["entries"], "receiving")
    assert entry["tasks"] == {"yahoo_price": {"a01": 1708, "b02": 900}}   # 空の項目は控えない

    pr.record_attempt(rid, {"yahoo_price": {"b02": 900}},
                      [{"ステップ": "⑤ Yahoo 販売価格", "対象": "1件", "状態": "失敗",
                        "メッセージ": "x"}])
    (entry,) = pr.open_of(store["entries"])
    assert entry["tasks"] == {"yahoo_price": {"b02": 900}}
    # 最初に失敗した分は書き換えない
    assert entry["original_tasks"] == {"yahoo_price": {"a01": 1708, "b02": 900}}

    pr.record_attempt(rid, {}, [])
    assert pr.open_of(store["entries"]) == []
    (entry,) = store["entries"]                       # 済んでも消えない
    assert entry["status"] == pr.DONE and entry["closed_at"]
    assert [a["remaining"] for a in entry["attempts"]] == [1, 0]


def test_失敗が無ければ控えを作らない(store):
    assert pr.add("pricing", {"ne_price": [], "yahoo_price": {}}) is None
    assert store["entries"] == []


def test_取り下げは理由が必須で消さずに残す(store):
    rid = pr.add("pricing", {"yahoo_price": {"a01": 100}})
    with pytest.raises(ValueError):
        pr.withdraw(rid, "  ")
    pr.withdraw(rid, "手動で反映済み")
    (entry,) = store["entries"]
    assert entry["status"] == pr.WITHDRAWN and entry["note"] == "手動で反映済み"
    assert pr.open_of(store["entries"]) == []


def test_知らないidへの書き戻しは黙って捨てない(store):
    with pytest.raises(pr.StoreError):
        pr.record_attempt("no-such-id", {})


def test_画面別に取り出せる(store):
    pr.add("receiving", {"yahoo_price": {"a": 1}})
    pr.add("pricing", {"yahoo_price": {"b": 2}})
    assert len(pr.open_of(store["entries"])) == 2
    assert len(pr.open_of(store["entries"], "pricing")) == 1


def test_壊れた控えを0件として読まない(monkeypatch):
    from lib.invoice import drive_master
    monkeypatch.setattr(pr, "_folder", lambda: "folder")
    monkeypatch.setattr(drive_master, "find_file", lambda name, folder: {"id": "x"})
    monkeypatch.setattr(drive_master, "download_bytes", lambda fid: b"{ broken")
    with pytest.raises(pr.StoreError):
        pr.load()
    monkeypatch.setattr(drive_master, "download_bytes", lambda fid: b'{"not": "a list"}')
    with pytest.raises(pr.StoreError):
        pr.load()


def test_控えが無いのは0件(monkeypatch):
    from lib.invoice import drive_master
    monkeypatch.setattr(pr, "_folder", lambda: "folder")
    monkeypatch.setattr(drive_master, "find_file", lambda name, folder: None)
    assert pr.load() == []


def test_再実行に渡す形は保存と読み戻しで変わらない():
    """控えから読み戻したものを、そのまま実行関数に渡せること。"""
    tasks = {
        "ne_main": [{"syohin_code": "a-01", "location": "120B-FAST", "org1": "120"}],
        "rakuten_price": [{"商品管理番号": "a", "sku_prices": {"sku1": 1708}, "対象コード": ["a-01"]}],
        "yahoo_price": {"a": 1708},
        "yahoo_delivery": {"rows": [{"code": "a", "便種": "宅配便"}],
                           "group_no": {"宅配便": "1"}, "group_bins": {}, "folder": "f"},
    }
    entry = pr.new_entry("receiving", tasks)
    assert json.loads(json.dumps(entry, ensure_ascii=False))["tasks"] == tasks
    assert pr.count_units(tasks) == 4
    text = "\n".join(pr.describe(tasks))
    for word in ("a-01→120B-FAST", "a→1708円", "a→宅配便", "1SKU"):
        assert word in text


def test_経過日数は読めなければNoneで0日に丸めない():
    now = datetime.datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
    assert pr.age_days({"created_at": "2026-10-07T05:00:00+00:00"}, now) == 1
    assert pr.age_days({"created_at": "2026-10-07T05:00:00"}, now) == 1   # 時差なしはUTC扱い
    assert pr.age_days({"created_at": "こわれた"}, now) is None
    assert pr.age_days({}, now) is None


# ---------------------------------------------------------------- 滞留監視

def _watch():
    from batch import pending_retry_watch
    return pending_retry_watch


@pytest.fixture
def _real_streamlit():
    """バッチはstreamlitをシムに差し替えるので、テスト後に必ず戻す。"""
    real = sys.modules.get("streamlit")
    yield
    if real is not None:
        sys.modules["streamlit"] = real
    else:
        sys.modules.pop("streamlit", None)


def _entry(created, status=pr.OPEN):
    e = pr.new_entry("receiving", {"yahoo_price": {"a": 1}}, "テスト")
    e["created_at"], e["status"] = created, status
    return e


def test_監視_無ければ正常(_real_streamlit):
    now = datetime.datetime(2026, 10, 8, 23, 0, tzinfo=UTC)
    code, message, _ = _watch().judge([], now)
    assert code == 0
    code, _, _ = _watch().judge([_entry("2026-10-01T00:00:00+00:00", pr.DONE)], now)
    assert code == 0                                   # 済んだものは古くても数えない


def test_監視_当日の分は件数を出すだけ(_real_streamlit):
    now = datetime.datetime(2026, 10, 7, 23, 0, tzinfo=UTC)
    code, message, lines = _watch().judge([_entry("2026-10-07T05:00:00+00:00")], now)
    assert code == 0 and "1件" in message and len(lines) == 1


def test_監視_1日残ったら赤(_real_streamlit):
    now = datetime.datetime(2026, 10, 8, 23, 0, tzinfo=UTC)
    code, message, lines = _watch().judge([_entry("2026-10-07T05:00:00+00:00")], now)
    assert code == 1 and "1日間" in message
    assert "a→1円" in lines[0]                         # 何が残っているかが通知に出る


def test_監視_日時が読めないものは正常にしない(_real_streamlit):
    now = datetime.datetime(2026, 10, 7, 23, 0, tzinfo=UTC)
    code, message, _ = _watch().judge([_entry("こわれた")], now)
    assert code == 1
