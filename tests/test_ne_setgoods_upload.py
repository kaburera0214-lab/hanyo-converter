# -*- coding: utf-8 -*-
"""NEセット商品マスタ登録バッチの計画CSVの読み先（batch/ne_setgoods_upload.load_plan_text）。

守りたいこと:
  - shared:<名前> は共通の置き場（Supabase shared_secrets）から読み、ドライブには触らない
    （このリポジトリのGoogle認可は drive.file で、PCがドライブに置いた計画は見えないため。2026-11-27）
  - 共通の置き場に無い・形が違うときは None（「見つからない」で止まる。空の計画として進めない）
  - それ以外の名前は今までどおりドライブから読む
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch import ne_setgoods_upload as u  # noqa: E402

PLAN = "セット商品コード,セット商品名,セット商品販売価格,商品コード,数量,代表商品コード\nset-a,セットA,3880,train-01,1,page-x\n"


@pytest.fixture
def drive(monkeypatch):
    calls = []

    def find_file(name, folder):
        calls.append(("find", name, folder))
        return {"id": "f1"} if name == "plan.csv" else None

    def download_bytes(fid):
        calls.append(("download", fid))
        return ("﻿" + PLAN).encode("utf-8")

    monkeypatch.setattr(u.drive_master, "find_file", find_file)
    monkeypatch.setattr(u.drive_master, "download_bytes", download_bytes)
    return calls


def test_shared_name_reads_the_shared_store_and_not_the_drive(monkeypatch, drive):
    asked = []
    monkeypatch.setattr(u, "_shared_get", lambda name: asked.append(name) or {"csv": "﻿" + PLAN})
    assert u.load_plan_text("shared:ne_setgoods_plan_sgift", "folder") == PLAN
    assert asked == ["ne_setgoods_plan_sgift"]
    assert drive == []


@pytest.mark.parametrize("stored", [None, {}, {"csv": None}, {"text": PLAN}])
def test_missing_or_odd_shared_plan_is_not_found(monkeypatch, drive, stored):
    monkeypatch.setattr(u, "_shared_get", lambda name: stored)
    assert u.load_plan_text("shared:x", "folder") is None
    assert drive == []


def test_plain_name_still_reads_the_drive(monkeypatch, drive):
    monkeypatch.setattr(u, "_shared_get", lambda name: pytest.fail("共通の置き場を読んではいけない"))
    assert u.load_plan_text("plan.csv", "folder") == PLAN
    assert u.load_plan_text("nothing.csv", "folder") is None


def test_run_stops_before_ne_when_the_plan_is_missing(monkeypatch, drive):
    monkeypatch.setattr(u, "_shared_get", lambda name: None)
    monkeypatch.setattr(u.master_store, "folder_id", lambda: "folder")
    monkeypatch.setattr(u.setgoods, "execute", lambda *a, **k: pytest.fail("NEへ進んではいけない"))
    assert u.run("shared:x", [], True, True) == 1
