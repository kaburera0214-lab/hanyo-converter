# -*- coding: utf-8 -*-
"""便種・個口の変更（pages/24）：番号はバーコードのスキャンで入れ、どちらの注文かは読んだ値で決まる（2026-11-26）。
Team EC（Eシス）は J番号で「実行」だけ。スキャナーが最後に送る改行で「確かめる」「実行」が押されないこと。"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_page24_change_restart import setup, _texts  # noqa: E402,F401
from lib.scan_code import parse  # noqa: E402


@pytest.fixture
def sent(monkeypatch):
    from lib import master_store
    from lib.invoice import drive_master
    box = []
    monkeypatch.setattr(master_store, "folder_id", lambda: "root")
    monkeypatch.setattr(drive_master, "get_or_create_folder", lambda *a, **k: "folder")
    monkeypatch.setattr(drive_master, "upload_bytes", lambda data, name, *a, **k: box.append((name, json.loads(data))))
    return box


# 実物（2026-11-18〜20 の納品書・ピッキングリストのPDF）から読んだバーコードの中身
@pytest.mark.parametrize("raw,want", [
    ("D0000201722D", ("ne", "201722")),      # NEの納品書：NW-7、両端のDつき
    ("0000201722", ("ne", "201722")),        # 両端のDを送らないスキャナー
    ("0000201722\r\n", ("ne", "201722")),    # 最後の改行
    ("201722", ("ne", "201722")),            # 手で打った
    ("J81583", ("te", "J81583")),            # Eシスのピッキングリスト：Code 93
    ("j81583", ("te", "J81583")),
    ("J8158345", ("te", "J81583")),          # チェック用の2文字まで送るスキャナー（数字のチェック文字）
    ("J815936A", ("te", "J81593")),
    ("J81526%V", ("te", "J81526")),
])
def test_スキャンの値を番号に直す(raw, want):
    assert parse(raw) == want


@pytest.mark.parametrize("raw", ["X1", "0000000000", "J8", "425405-20261123-0586721273"])
def test_読めない値は止める(raw):
    kind, why = parse(raw)
    assert kind == "" and why


def _scan(at, value):
    at.run()
    at.text_input[0].set_value(value).run()     # スキャナーの改行＝値の確定だけ
    return at


def test_スキャンしただけでは何も送らない(setup, sent):
    at, box = setup
    _scan(at, "D0000201722D\n")
    assert not at.exception, at.exception
    assert sent == []
    assert "パピー（NEの伝票） 201722" in _texts(at)
    assert at.button[0].label == "確かめる" and not at.button[0].disabled


def test_NEの納品書をスキャンして確かめる(setup, sent):
    at, box = setup
    _scan(at, "D0000201722D").button[0].click().run()
    assert len(sent) == 1 and sent[0][1]["cmd"] == "check" and sent[0][1]["order_id"] == "201722"


def test_ピッキングリストをスキャンするとTeamECになる(setup, sent):
    at, box = setup
    _scan(at, "J81583")
    assert "Team EC（Eシスの注文） J81583" in _texts(at)
    assert set(at.selectbox[0].options) == {"ヤマト 宅急便", "ヤマト ネコポス"}
    assert at.button[0].label == "実行"
    at.button[0].click().run()
    name, body = sent[0]
    assert name.endswith("_te_run.json") and body["order_id"] == "J81583" and body["te"] is True


def test_読めない値ではボタンを押せない(setup, sent):
    at, box = setup
    _scan(at, "X1")
    assert "読めない番号" in _texts(at)
    assert at.button[0].disabled and sent == []


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
