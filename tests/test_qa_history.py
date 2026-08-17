# -*- coding: utf-8 -*-
"""
質問・回答管理の編集履歴（lib/qa/history）の純関数テスト。
実行: hanyo-converter直下で  python tests/test_qa_history.py
"""
import os
import sys
from datetime import datetime, timedelta

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


def test_履歴は間引かれない():
    """以前は30行で古い行を捨てていた。証跡なので全部残す。"""
    lines = [f"[2026-08-11 14:05] パピー：回答{i}" for i in range(200)]
    kept = h.trim_history(lines)
    assert len(kept.split("\n")) == 200, len(kept.split("\n"))
    assert "回答0" in kept and "回答199" in kept
    print("OK 履歴は間引かれない")


def test_追記を繰り返しても古い行が残る():
    hist = ""
    for i in range(60):
        hist = h.append_history(hist, "追加回答", at=AT)
    assert len(hist.split("\n")) == 60
    print("OK 追記を繰り返しても古い行が残る")


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


def test_undo_window():
    done_at = datetime(2026, 8, 12, 9, 0, tzinfo=h.JST)
    hist = "\n".join([
        "[2026-08-11 11:30] パピー：回答",
        "[2026-08-12 09:00] インハナ：完了",
    ])
    assert h.last_action_at(hist, "完了") == done_at
    # 完了直後 → 取消できる
    left = h.undo_remaining(hist, now=done_at + timedelta(minutes=30))
    assert left is not None and int(left.total_seconds() // 60) == 150
    # 期限ちょうど・過ぎたあと → 取消不可
    assert h.undo_remaining(hist, now=done_at + timedelta(hours=h.UNDO_WINDOW_HOURS)) is None
    assert h.undo_remaining(hist, now=done_at + timedelta(days=1)) is None
    # 完了行が無い古いデータ → 取消不可
    assert h.undo_remaining("[2026-08-11 11:30] パピー：回答", now=done_at) is None
    assert h.undo_remaining("", now=done_at) is None
    # 完了→取消→再完了。最後の完了が基準になる
    redone = hist + "\n[2026-08-12 09:10] インハナ：完了取消\n[2026-08-12 15:00] インハナ：完了"
    assert h.last_action_at(redone, "完了") == datetime(2026, 8, 12, 15, 0, tzinfo=h.JST)
    print("OK undo window")


def test_retag_empty():
    assert h.retag_history("") == ("", 0)
    assert h.retag_history(None) == ("", 0)
    print("OK retag empty")


if __name__ == "__main__":
    test_actor_by_action()
    test_append()
    test_trim()
    test_retag()
    test_undo_window()
    test_retag_empty()
    print("\nすべて通過")
