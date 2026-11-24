# -*- coding: utf-8 -*-
"""NE受注明細（ne-order-rows）は GapBoard の朝の同期が合図の push で起動する（2026-11-24〜）。

時刻指定（cron 5:30）は 9:30〜14:00 に遅れ、GapBoard の 6:30 の同期が前日の明細で原価を計算していた。
cron に戻すと同じことが起きる。合図の枝にはワークフローの写ししか無いので、コードは main から取る。
"""
import os

import yaml

WF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  ".github", "workflows", "ne-order-rows.yml")


def _load():
    with open(WF, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    return d.get("on") or d.get(True)     # YAML 1.1 では on が True と読まれる


def test_started_by_push_to_trigger_branch():
    on = _load()
    assert on["push"]["branches"] == ["ne-order-rows-trigger"]
    assert "schedule" not in on           # 時刻指定は遅れるので使わない
    assert "workflow_dispatch" in on      # 過去分の取り込み・手動用は残す


def test_code_comes_from_main():
    with open(WF, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    steps = d["jobs"]["export"]["steps"]
    co = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")][0]
    assert co["with"]["ref"] == "main"
