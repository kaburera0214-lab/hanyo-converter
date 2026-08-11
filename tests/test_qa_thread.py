# -*- coding: utf-8 -*-
"""
質問・回答管理の会話ログ整形（lib/qa/thread_ui）の純関数テスト。
実行: hanyo-converter直下で  python tests/test_qa_thread.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.qa import thread_ui as tu  # noqa: E402

# 実際に見づらくなった例（価格改定ツールのテスト）を縮めたもの
SAMPLE = """（古い会話を省略）
【Q】価格改定ツールのテストを実施しました。
■ボタン表示に関する確認
「楽天から現在価格を取得」ボタンが表示されておりません。

-----------------
価格改定ツールを作成しました。
週明けで構いませんので、受注業務後にテストをお願いしたいです。
-----------------

【A】はい、作成したCSVをアップロードしたらボタンが表示される仕組みです。

【追加Q｜2026-08-10 16:22】「JAN列が見つかりません。」というエラーが生じました。
https://drive.google.com/file/d/1DtMXbck7vF/view?usp=drive_link

【追加A｜2026-08-10 19:26】ヘッダーがないからですね。
＞・入力CSVの作成（JAN、新下代のみ）を作る
ここで伝えた「JAN」「新下代」をヘッダーにして取り込んでください。

【追加Q｜2026-08-11 11:16】＞ここで伝えた「JAN」「新下代」をヘッダーにして取り込んでください。
ここで伝えた「JAN」「新下代」をヘッダーにして取り込んでください。
はい、作成したCSVをアップロードしたらボタンが表示される仕組みです。
➡承知致しました。教えて頂きありがとうございます。
算出した価格が一致されていないようです。"""


def test_parse():
    turns, preamble = tu.parse_conversation(SAMPLE)
    assert preamble == "（古い会話を省略）", preamble
    assert [t["kind"] for t in turns] == ["Q", "A", "追加Q", "追加A", "追加Q"], turns
    assert [t["role"] for t in turns] == ["q", "a", "q", "a", "q"]
    assert turns[2]["timestamp"] == "2026-08-10 16:22"
    assert turns[0]["timestamp"] == ""
    assert turns[4]["body"].endswith("算出した価格が一致されていないようです。")
    print("OK parse")


def test_separator_block_is_quoted():
    turns, _ = tu.parse_conversation(SAMPLE)
    seen = set()
    segs = tu.split_body(turns[0]["body"], seen)
    kinds = [k for k, _ in segs]
    assert "quote" in kinds, segs
    quoted = "\n".join(t for k, t in segs if k == "quote")
    assert "価格改定ツールを作成しました。" in quoted
    assert "-----------------" not in quoted  # 区切り線自体は消える
    main = "\n".join(t for k, t in segs if k == "main")
    assert "ボタン表示に関する確認" in main
    print("OK separator")


def test_repeat_is_folded():
    """3ラリー目の再掲・引用がすべて quote に落ち、新しい文だけ main に残る。"""
    turns, _ = tu.parse_conversation(SAMPLE)
    seen = set()
    for t in turns[:-1]:
        tu.split_body(t["body"], seen)
    segs = tu.split_body(turns[-1]["body"], seen)
    main = "\n".join(t for k, t in segs if k == "main")
    quoted = "\n".join(t for k, t in segs if k == "quote")
    assert "承知致しました" in main
    assert "算出した価格が一致されていない" in main
    assert "ヘッダーにして取り込んでください" not in main, main
    assert "ボタンが表示される仕組みです" not in main, main
    assert "ヘッダーにして取り込んでください" in quoted
    print("OK dedupe")


def test_short_line_survives():
    seen = {"はい"}
    segs = tu.split_body("はい\n了解しました", seen)
    assert all(k == "main" for k, _ in segs), segs
    print("OK short line")


def test_escape_and_link():
    html = tu.text_to_html("<b>a</b>\nhttps://example.com/x?y=1 です")
    assert "&lt;b&gt;" in html
    assert '<a href="https://example.com/x?y=1"' in html
    assert "<br>" in html
    print("OK escape")


if __name__ == "__main__":
    test_parse()
    test_separator_block_is_quoted()
    test_repeat_is_folded()
    test_short_line_survives()
    test_escape_and_link()
    print("\nすべて通過")
