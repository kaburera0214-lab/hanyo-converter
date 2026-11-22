# -*- coding: utf-8 -*-
"""NE API の月の枠の割り振り（lib/ne_api/budget_plan.py）と、内訳・ペースの記録（lib/ne_api/usage.py）のテスト。

無料枠は全ツールで分け合う1つの枠（2026-11-22 ユーザー指摘）。ツールを足したのに割り振りに書いていない・
割り振りの合計が枠を超えた、を設計の時点で落とす。"""
import datetime
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.ne_api import budget_plan as bp  # noqa: E402
from lib.ne_api import usage  # noqa: E402


def test_plan_fits_free_limit():
    # ルーティンの見込み＋単発の枠が無料枠に収まる。超えるなら、作る前に設計を変えるか有料の段を決める
    assert bp.headroom() >= 0, "割り振りの合計 {}＋単発 {} が無料枠 {} を超えています".format(
        bp.routine_total(), bp.ONEOFF_RESERVE, bp.FREE_LIMIT)


def test_every_routine_has_reason():
    for name, v in bp.ROUTINE.items():
        assert v["monthly"] > 0 and v["why"], name


def _ne_callers():
    """NE API を呼ぶバッチと画面（ファイル名＝usage が数える名前）。"""
    names = set()
    for path in glob.glob(os.path.join(ROOT, "batch", "*.py")) + glob.glob(os.path.join(ROOT, "pages", "*.py")):
        src = open(path, encoding="utf-8").read()
        if re.search(r"lib\.ne_api|from lib\.ne_api|ne_api import", src):
            names.add(os.path.splitext(os.path.basename(path))[0])
    return names - {"st_shim", "weekend_ship_ne"}     # 土日祝出荷は set_caller で「土日祝出荷:種類」と名乗る


def test_every_ne_caller_is_in_plan():
    """NE API を呼ぶバッチ・画面は、ルーティンか単発のどちらかに書いてある（未登録で数えない）。"""
    missing = sorted(n for n in _ne_callers() if bp.classify(n) == "unknown")
    assert not missing, "lib/ne_api/budget_plan.py の ROUTINE か ONEOFF に書いてください: {}".format(missing)


def test_weekend_ship_kinds():
    from batch import weekend_ship_ne as w
    for kind in w.JOBS:
        assert bp.classify("土日祝出荷:" + kind) in ("routine", "oneoff")
    assert bp.classify("土日祝出荷:apply") == "routine"
    assert bp.classify("土日祝出荷:order_upload") == "oneoff"      # イレギュラー受注の起票＝依頼
    assert bp.classify("土日祝出荷:apply", oneoff=True) == "oneoff"  # ドライラン＝試験
    assert bp.classify("だれか") == "unknown"


def test_caller_key_marks_manual_runs_as_oneoff(monkeypatch):
    usage.set_caller("ne_order_rows")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    assert usage._caller_key() == "ne_order_rows" + usage.ONEOFF_MARK
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    assert usage._caller_key() == "ne_order_rows"
    usage.set_caller("x", oneoff=True)
    assert usage._caller_key() == "x" + usage.ONEOFF_MARK
    usage.set_caller("")


def test_merge_keeps_breakdown_by_day_and_resets_monthly():
    now = datetime.datetime(2026, 11, 22, 10, 0)
    d = usage.merge({}, {"calls": 3, "bytes": 10, "by": {"ne_order_rows": 2, "土日祝出荷:apply": 1}}, now)
    d = usage.merge(d, {"calls": 1, "bytes": 0, "by": {"ne_order_rows": 1}}, now)
    assert d["calls"] == 4 and d["by"]["ne_order_rows"] == 3
    assert d["days"]["2026-11-22"]["土日祝出荷:apply"] == 1
    d2 = usage.merge(d, {"calls": 1, "bytes": 0, "by": {"x": 1}}, datetime.datetime(2026, 12, 1, 0, 5))
    assert d2["calls"] == 1 and d2["prev_calls"] == 4 and d2["prev_by"]["ne_order_rows"] == 3


def test_pace_projects_routine_only_and_flags_tool():
    now = datetime.datetime(2026, 11, 10, 12, 0)   # 30日の月の10日目
    data = {"calls": 500, "by": {"土日祝出荷:apply": 300, "ne_order_rows": 20,
                                 "21_📥_入荷登録" + usage.ONEOFF_MARK: 80, "だれか": 100}}
    rep = usage.pace_report(data, now)
    assert rep["routine"] == 320 and rep["oneoff"] == 80 and rep["unknown"] == 100
    assert rep["projected"] == 320 * 3 + 80 + 100            # 単発・未登録は月末まで延ばさない
    ws = next(t for t in rep["tools"] if t["tool"] == "土日祝出荷")
    assert ws["over"]                                          # 10日目で300回＞割り振り約83回の1.5倍
    marks = [m for m, _ in usage.pace_alerts(data, now)]
    assert "projected" in marks and "tool:土日祝出荷" in marks
    data["alerted_pace"] = marks
    assert usage.pace_alerts(data, now) == []                  # 月内は同じ通知を繰り返さない


def test_pace_counts_calls_before_breakdown_as_unknown():
    rep = usage.pace_report({"calls": 927, "by": {}}, datetime.datetime(2026, 11, 22))
    assert rep["unknown"] == 927 and rep["unknown_callers"][0][0] == "（内訳を取る前）"


def test_no_projection_alert_early_in_month():
    """月初は1日の多寡で外れやすいので、月末の見込みの通知は5日目から。ツールの使いすぎは月初でも知らせる"""
    data = {"calls": 100, "by": {"ne_order_rows": 100}}
    marks = [m for m, _ in usage.pace_alerts(data, datetime.datetime(2026, 11, 2))]
    assert marks == ["tool:ne_order_rows"]
