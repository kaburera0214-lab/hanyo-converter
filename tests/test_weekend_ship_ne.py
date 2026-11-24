# -*- coding: utf-8 -*-
"""土日祝出荷の NE 担当（batch/weekend_ship_ne.py）の、通信しない部分のテスト。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import weekend_ship_ne as w  # noqa: E402


def test_update_xml_escapes_text():
    xml = w.update_xml({"receive_order_worker_text": "【土日保留】<住所>&確認"})
    assert "&lt;住所&gt;&amp;確認" in xml
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?><root><receiveorder_base>')


def test_update_xml_rejects_unlisted_fields():
    # PC側の取り違えで、住所や金額などを書き換えない
    with pytest.raises(ValueError):
        w.update_xml({"receive_order_consignee_address1": "x"})


def test_budget_stops():
    b = w.Budget(0)
    with pytest.raises(RuntimeError):
        b.call("api_v1_login_company/info")


def test_ne_screen_is_never_touched():
    # NE 利用規約 第24条1項(17)：画面の自動操作はしない。API のホスト以外を呼ばない
    src = open(w.__file__, encoding="utf-8").read()
    for bad in ("main.next-engine.com", "base.next-engine.org/users", "playwright", "selenium"):
        assert bad not in src


def test_idle_release_after_work_only():
    # 処理したあとは10分、指示が来ないまま起動した担当は2分で席を空ける（居座らない）
    assert w.idle_action(599, handled=3) is None
    assert w.idle_action(601, handled=3) == "release"
    assert w.idle_action(119, handled=0) is None
    assert w.idle_action(121, handled=0) == "release"


def test_ne_workflows_queue_first_come_first_served():
    # 同じ順番に並ぶワークフローは全部 queue: max。1本でも欠けると、待っているバッチが取り消される
    import glob
    found = []
    for p in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
        s = open(p, encoding="utf-8").read()
        if "group: ne-keepalive" in s:
            found.append(os.path.basename(p))
            assert "queue: max" in s and "cancel-in-progress: false" in s, p
    assert len(found) >= 8


def test_stale_jobs_are_not_run():
    import datetime as dt
    now = dt.datetime(2026, 11, 23, 12, 0, 0, tzinfo=w.JST)
    assert w.job_is_stale({"id": "20261123_115600_exit_abc", "kind": "exit"}, now)      # 終了は3分まで
    assert not w.job_is_stale({"id": "20261123_115830_exit_abc", "kind": "exit"}, now)
    assert not w.job_is_stale({"id": "20261123_113500_apply_abc", "kind": "apply"}, now)
    assert w.job_is_stale({"id": "20261122_113500_ship_abc", "kind": "ship"}, now)      # 前日の出荷確定は実行しない
    assert not w.job_is_stale({"id": "manual", "kind": "ping"}, now)
