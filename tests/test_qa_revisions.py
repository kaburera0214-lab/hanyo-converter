# -*- coding: utf-8 -*-
"""
修正前の版を積む処理（lib/qa/revisions）のテスト。
守りたいのは「回答修正・質問編集で上書きしない」の1点。
実行: hanyo-converter直下で  python tests/test_qa_revisions.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.qa import revisions as R  # noqa: E402
from lib.qa.history import JST  # noqa: E402

AT1 = datetime(2026, 8, 17, 10, 30, tzinfo=JST)
AT2 = datetime(2026, 8, 17, 14, 0, tzinfo=JST)


def test_回答修正で修正前の本文が残る():
    before = "送料はお客様負担でお願いします。"
    hist = R.push_revision("", R.KIND_ANSWER, before, actor="山田", at=AT1)
    assert before in hist
    got = R.parse_revisions(hist)
    assert len(got) == 1
    assert got[0]["種別"] == "回答修正前"
    assert got[0]["日時"] == "2026-08-17 10:30"
    assert got[0]["記録者"] == "山田"
    assert got[0]["本文"] == before


def test_版は積み重なり新しいものが先頭():
    hist = R.push_revision("", R.KIND_ANSWER, "1回目の本文", actor="山田", at=AT1)
    hist = R.push_revision(hist, R.KIND_ANSWER, "2回目の本文", actor="佐藤", at=AT2)
    got = R.parse_revisions(hist)
    assert [r["本文"] for r in got] == ["2回目の本文", "1回目の本文"]
    assert [r["記録者"] for r in got] == ["佐藤", "山田"]


def test_空の本文は積まない():
    """初回入力を「修正前の版」として積んでしまわないこと。"""
    assert R.push_revision("", R.KIND_ANSWER, "", actor="山田", at=AT1) == ""
    assert R.push_revision("", R.KIND_ANSWER, "  　\n", actor="山田", at=AT1) == ""
    hist = R.push_revision("", R.KIND_ANSWER, "本文", at=AT1)
    assert R.push_revision(hist, R.KIND_ANSWER, "", at=AT2) == hist


def test_質問編集はタイトルとタグも残る():
    snap = R.question_snapshot("CS・返品について", "返品の送料はどちら負担ですか", ["CS", "受注"])
    hist = R.push_revision("", R.KIND_QUESTION, snap, actor="インハナ", at=AT1)
    got = R.parse_revisions(hist)[0]
    assert got["種別"] == "質問修正前"
    assert "タイトル: CS・返品について" in got["本文"]
    assert "タグ: CS・受注" in got["本文"]
    assert "返品の送料はどちら負担ですか" in got["本文"]


def test_タグ無しでも組める():
    snap = R.question_snapshot("タイトル", "本文", [])
    assert "タグ:" not in snap
    assert "本文" in snap


def test_複数行の本文が壊れない():
    body = "対応方法\n\n・1つ目\n・2つ目\n\n以上です。"
    hist = R.push_revision("", R.KIND_ANSWER, body, actor="山田", at=AT1)
    assert R.parse_revisions(hist)[0]["本文"] == body


def test_記録者が未指定なら不明():
    hist = R.push_revision("", R.KIND_ANSWER, "本文", at=AT1)
    assert R.parse_revisions(hist)[0]["記録者"] == "不明"


def test_空と壊れた履歴():
    assert R.parse_revisions("") == []
    assert R.parse_revisions(None) == []
    # 書式に合わないものは捨てずに1件として返す
    got = R.parse_revisions("書式に合わない昔のテキスト")
    assert len(got) == 1 and got[0]["本文"] == "書式に合わない昔のテキスト"


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
