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


# ── 先頭が切られた会話ログ（旧仕様の残骸）の扱い ──────────────────────
# 1900文字上限で先頭を捨てていたため、話者マーカーごと消えて本文だけが
# preamble に残る質問が7件ある。中央寄せの注記に流れ込んで会話に見えなくなっていた。
TRUNCATED = """（古い会話を省略）ようお願いいたします。
➡はい、承知致しました。

■確定フォルダの確認
＞下記フォルダのデータを反映しました。念のためご確認をお願いします。
➡ご反映頂きありがとうございます。

【追加A｜2026-08-13 23:53】超郷さんがやったことなくても、御社にノウハウがあるだろうから
「ゼロからの質問ではなく」という形式で確認をご提示いただく形ですと、判断・回答が可能です。

【追加Q｜2026-08-14 12:44】かしこまりました。今後、社内で確認した上、質問するようにします。"""


def test_先頭が切られた会話は注記と断片に分かれる():
    turns, preamble = tu.parse_conversation(TRUNCATED)
    assert [t["kind"] for t in turns] == ["追加A", "追加Q"], [t["kind"] for t in turns]
    note, orphan = tu.split_preamble(preamble)
    assert note == tu.OMITTED_NOTE
    # 会話の中身は注記ではなく断片side に来る（吹き出しで描かれる）
    assert "確定フォルダの確認" in orphan
    assert tu.OMITTED_MARK not in orphan
    assert "ようお願いいたします" in orphan
    print("OK 先頭が切られた会話は注記と断片に分かれる")


def test_断片の中の引用は畳まれる():
    _turns, preamble = tu.parse_conversation(TRUNCATED)
    _note, orphan = tu.split_preamble(preamble)
    kinds = [k for k, _ in tu.split_body(orphan, set())]
    assert "quote" in kinds, kinds   # ＞で始まる行は引用として畳む
    assert "main" in kinds
    print("OK 断片の中の引用は畳まれる")


def test_省略マークが無い前置きはそのまま():
    turns, preamble = tu.parse_conversation("なにかの前置き\n【Q】質問\n【A】回答")
    assert [t["kind"] for t in turns] == ["Q", "A"]
    note, orphan = tu.split_preamble(preamble)
    assert note == ""
    assert orphan == "なにかの前置き"
    print("OK 省略マークが無い前置きはそのまま")


def test_前置きが無ければ両方空():
    assert tu.split_preamble("") == ("", "")
    assert tu.split_preamble(None) == ("", "")
    # 省略マークの直後にマーカーが続く場合（断片が0字）
    assert tu.split_preamble("（古い会話を省略）") == (tu.OMITTED_NOTE, "")
    print("OK 前置きが無ければ両方空")


def test_短い引用は畳まない():
    """1〜2行の引用は開く手間の方が重いので、そのまま薄字で見せる。"""
    html = tu._quote_html("＞下記フォルダのデータを反映しました。")
    assert "<details" not in html, html
    assert "qa-quote-open" in html
    assert "下記フォルダのデータを反映しました" in html
    print("OK 短い引用は畳まない")


def test_長い引用は畳む():
    long_quote = "\n".join(f"引用の{i}行目です" for i in range(6))
    html = tu._quote_html(long_quote)
    assert "<details" in html
    assert "引用・過去のやり取り（6行）を表示" in html
    print("OK 長い引用は畳む")


def test_境界は2行まで畳まない():
    two = tu._quote_html("1行目のながい引用文\n2行目のながい引用文")
    three = tu._quote_html("1行目のながい引用文\n2行目のながい引用文\n3行目のながい引用文")
    assert "<details" not in two
    assert "<details" in three
    print("OK 境界は2行まで畳まない")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                fails += 1
                print(f"NG   {name}: {e}")
    print("---")
    print("全部通りました" if not fails else f"{fails}件 失敗")
    sys.exit(1 if fails else 0)
