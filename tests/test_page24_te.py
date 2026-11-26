# -*- coding: utf-8 -*-
"""便種・個口の変更（pages/24）：Team EC（Eシス）の注文はプルダウンで分け、J番号で「実行」だけ（2026-11-26）。"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_page24_change_restart import setup, _texts  # noqa: E402,F401


@pytest.fixture
def sent(monkeypatch):
    from lib import master_store
    from lib.invoice import drive_master
    box = []
    monkeypatch.setattr(master_store, "folder_id", lambda: "root")
    monkeypatch.setattr(drive_master, "get_or_create_folder", lambda *a, **k: "folder")
    monkeypatch.setattr(drive_master, "upload_bytes", lambda data, name, *a, **k: box.append((name, json.loads(data))))
    return box


def _fill(at, target, number):
    at.run()
    at.selectbox[0].set_value(target).run()
    at.text_input[0].set_value(number)
    at.button[0].click().run()


def test_TEはJ番号で実行だけを送る(setup, sent):
    at, box = setup
    _fill(at, "Team EC（Eシスの注文）", "j81828")
    assert not at.exception, at.exception
    assert len(sent) == 1
    name, body = sent[0]
    assert name.endswith("_te_run.json")
    assert body["order_id"] == "J81828" and body["te"] is True and body["cmd"] == "te_run"
    assert at.session_state["chg_req"]["te"] is True


def test_TEの便は宅急便とネコポスだけ(setup, sent):
    at, box = setup
    at.run()
    at.selectbox[0].set_value("Team EC（Eシスの注文）").run()
    assert set(at.selectbox[1].options) == {"ヤマト 宅急便", "ヤマト ネコポス"}
    assert at.button[0].label == "実行"


def test_選び間違いは止める(setup, sent):
    at, box = setup
    _fill(at, "Team EC（Eシスの注文）", "201884")
    assert "パピー" in _texts(at) and sent == []
    at2, _ = setup
    _fill(at2, "パピー（NEの伝票）", "J81828")
    assert "Team EC" in _texts(at2) and sent == []


def test_TEの結果を出す(setup):
    at, box = setup
    now = time.time()
    box["state"] = {"requests": {"t1": {"status": "done", "message": "J81828 を ヤマト 宅急便・2個口 にしました。"}},
                    "started_ts": now - 3600, "published_ts": now}
    at.session_state["chg_req"] = {"req_id": "t1", "order_id": "J81828", "delivery_id": "20", "boxes": 2,
                                   "te": True, "sent_ts": now - 60}
    at.run()
    assert not at.exception, at.exception
    assert any("2個口 にしました" in e.value for e in at.success)
    assert "確かめています" not in _texts(at)
