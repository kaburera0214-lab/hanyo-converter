# -*- coding: utf-8 -*-
"""
質問・回答管理の編集履歴（lib/qa/history）の純関数テスト。
実行: hanyo-converter直下で  python tests/test_qa_history.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.qa import history as h  # noqa: E402

AT = datetime(2026, 8, 11, 14, 5, tzinfo=h.JST)


def test_actor_by_action():
    assert h.actor_for("追加質問") == "インハナ"
    assert h.actor_for("完了") == "インハナ"
    assert h.actor_for("完了取消") == "インハナ"
    assert h.actor_for("質問投稿") == "インハナ"
    assert h.actor_for("回答") == "パピー"
    assert h.actor_for("追加回答") == "パピー"
    assert h.actor_for("回答修正") == ""  # 認証した本人の名前を使う
    print("OK actor_for")


def test_append():
    first = h.append_history("", "質問投稿", at=AT)
    assert first == "[2026-08-11 14:05] インハナ：質問投稿", first
    second = h.append_history(first, "回答", at=AT)
    assert second.endswith("[2026-08-11 14:05] パピー：回答")
    assert len(second.split("\n")) == 2
    named = h.append_history("", "回答修正", actor="山田", at=AT)
    assert named == "[2026-08-11 14:05] 山田：回答修正", named
    print("OK append")


def test_trim():
    lines = [f"[2026-08-11 14:05] パピー：回答{i}" for i in range(40)]
    trimmed = h.trim_history(lines)
    assert len(trimmed.split("\n")) == h.MAX_LINES
    assert "回答39" in trimmed and "回答0" not in trimmed
    assert len(trimmed) <= h.MAX_CHARS
    print("OK trim")


def test_retag():
    before = "\n".join([
        "[2026-07-22 10:00] インハナ：質問投稿",
        "[2026-07-22 11:30] パピー：回答",
        "[2026-08-10 16:22] パピー：追加質問",   # 実際はインハナ
        "[2026-08-10 19:26] パピー：追加回答",
        "[2026-08-11 09:00] パピー：完了",       # 実際はインハナ
        "[2026-08-11 09:05] 山田：回答修正",     # 認証名はそのまま
        "書式に合わない行",                        # 触らない
    ])
    after, changed = h.retag_history(before)
    lines = after.split("\n")
    assert changed == 2, changed
    assert lines[0] == "[2026-07-22 10:00] インハナ：質問投稿"
    assert lines[1] == "[2026-07-22 11:30] パピー：回答"
    assert lines[2] == "[2026-08-10 16:22] インハナ：追加質問"
    assert lines[3] == "[2026-08-10 19:26] パピー：追加回答"
    assert lines[4] == "[2026-08-11 09:00] インハナ：完了"
    assert lines[5] == "[2026-08-11 09:05] 山田：回答修正"
    assert lines[6] == "書式に合わない行"
    # 冪等性（2回かけても変わらない）
    again, changed2 = h.retag_history(after)
    assert again == after and changed2 == 0
    print("OK retag")


def test_retag_empty():
    assert h.retag_history("") == ("", 0)
    assert h.retag_history(None) == ("", 0)
    print("OK retag empty")


if __name__ == "__main__":
    test_actor_by_action()
    test_append()
    test_trim()
    test_retag()
    test_retag_empty()
    print("\nすべて通過")
