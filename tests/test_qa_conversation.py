# -*- coding: utf-8 -*-
"""
会話ログ・回答本文の追記（lib/qa/conversation）と
rich_textの分割・連結（lib/qa/notion_text）のテスト。

守りたいのは「原則、全部残す」の1点。
実行: hanyo-converter直下で  python tests/test_qa_conversation.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.qa import conversation as C  # noqa: E402
from lib.qa import notion_text as NT  # noqa: E402
from lib.qa.history import JST  # noqa: E402
from lib.qa.thread_ui import parse_conversation  # noqa: E402

AT1 = datetime(2026, 8, 13, 10, 0, tzinfo=JST)
AT2 = datetime(2026, 8, 13, 15, 30, tzinfo=JST)


def test_回答本文は上書きされない():
    """これが今回いちばん直したかったところ。"""
    first = "セルタンはそのままでOKです。"
    merged = C.append_answer(first, "カワダも同様に進めてください。", at=AT1)
    assert first in merged, merged
    assert "カワダも同様に" in merged
    assert "【追加回答｜2026-08-13 10:00】" in merged

    # 2回目の追加回答でも、1回目までの内容が残る
    again = C.append_answer(merged, "3件目も同じ扱いです。", at=AT2)
    assert first in again
    assert "カワダも同様に" in again
    assert "3件目も同じ扱い" in again


def test_回答が空なら見出しをつけない():
    assert C.append_answer("", "はじめての回答", at=AT1) == "はじめての回答"
    assert C.append_answer("既存", "   ", at=AT1) == "既存"


def test_会話ログは文字数で切られない():
    """1900文字上限で先頭を捨てていた挙動が無くなっていること。"""
    log = C.start_log("あ" * 1500, "い" * 1500)
    log = C.append_turn(log, "追加Q", "う" * 1500, at=AT1)
    log = C.append_turn(log, "追加A", "え" * 1500, at=AT2)
    assert len(log) > 6000, len(log)
    assert "（古い会話を省略）" not in log
    assert log.startswith("【Q】あああ")          # 先頭が残っている
    assert "え" * 1500 in log                     # 末尾も残っている


def test_会話ログはスレッド表示の書式に合う():
    log = C.start_log("送料はどうしますか", "無料でお願いします")
    log = C.append_turn(log, "追加Q", "3980円未満でもですか", at=AT1)
    log = C.append_turn(log, "追加A", "はい、その場合も無料です", at=AT2)
    turns, preamble = parse_conversation(log)
    assert preamble == ""
    assert [t["kind"] for t in turns] == ["Q", "A", "追加Q", "追加A"]
    assert turns[2]["timestamp"] == "2026-08-13 10:00"
    assert turns[3]["body"] == "はい、その場合も無料です"


def test_空の追記は何もしない():
    log = C.start_log("質問", "回答")
    assert C.append_turn(log, "追加Q", "") == log
    assert C.append_turn(log, "追加Q", "  　") == log


def test_判断理由は追記しカテゴリは和集合():
    d = C.append_reason("Z発注の性質による", "お客様目線での確認も必要", at=AT1)
    assert "Z発注の性質による" in d and "お客様目線" in d
    assert C.merge_categories(["社内ルール"], ["顧客対応", "社内ルール"]) == ["社内ルール", "顧客対応"]
    assert C.merge_categories([], ["品質優先"]) == ["品質優先"]
    assert C.merge_categories(["品質優先"], []) == ["品質優先"]


def test_rich_textは分割して全文を往復する():
    text = "".join(chr(0x3042 + (i % 80)) for i in range(7000))
    chunks = NT.to_rich_text(text)
    assert len(chunks) == 4, len(chunks)                       # 1900文字ずつ
    assert all(len(c["text"]["content"]) <= 1900 for c in chunks)
    # Notionから読み戻した形を模して連結する
    prop = {"rich_text": [{"plain_text": c["text"]["content"]} for c in chunks]}
    assert NT.get_text(prop) == text


def test_rich_textの空と欠損():
    assert NT.to_rich_text("") == []
    assert NT.to_rich_text(None) == []
    assert NT.get_text({}) == ""
    assert NT.get_text(None) == ""
    assert NT.get_text({"rich_text": []}) == ""


def test_rich_textの上限を超えたら末尾を残す():
    text = "あ" * (NT.MAX_CHARS + 500)
    chunks = NT.to_rich_text(text)
    assert len(chunks) <= NT.MAX_CHUNKS + 1
    joined = "".join(c["text"]["content"] for c in chunks)
    assert joined.startswith("（このプロパティの上限を超えたため")
    assert joined.endswith("あ")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except AssertionError as e:
                fails += 1
                print(f"NG   {name}: {e}")
    print("---")
    print("全部通りました" if not fails else f"{fails}件 失敗")
    sys.exit(1 if fails else 0)
