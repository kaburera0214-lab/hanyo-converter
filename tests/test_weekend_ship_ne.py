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
    # 処理したあとは10分で席を空ける（後ろのバッチを取り消させない）。起動直後は75分待ってPC停止を知らせる
    assert w.idle_action(599, handled=3) is None
    assert w.idle_action(601, handled=3) == "release"
    assert w.idle_action(601, handled=0) is None
    assert w.idle_action(75 * 60 + 1, handled=0) == "alert"


def test_stale_jobs_are_not_run():
    import datetime as dt
    now = dt.datetime(2026, 11, 23, 12, 0, 0, tzinfo=w.JST)
    assert w.job_is_stale({"id": "20261123_115600_exit_abc", "kind": "exit"}, now)      # 終了は3分まで
    assert not w.job_is_stale({"id": "20261123_115830_exit_abc", "kind": "exit"}, now)
    assert not w.job_is_stale({"id": "20261123_113500_apply_abc", "kind": "apply"}, now)
    assert w.job_is_stale({"id": "20261122_113500_ship_abc", "kind": "ship"}, now)      # 前日の出荷確定は実行しない
    assert not w.job_is_stale({"id": "manual", "kind": "ping"}, now)
