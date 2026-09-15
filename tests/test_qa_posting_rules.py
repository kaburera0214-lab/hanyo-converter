# -*- coding: utf-8 -*-
"""記入ルールの正本（lib/qa/posting_rules.py）と、質問投稿ページの整合を検証する。

守りたいこと: 「運用ルールを変えたのに、画面の案内文だけ古いまま残る」を検知すること。
文言の間違いは普通のテストでは落ちないので、正本をページに突き合わせる形で見張る。

背景（2026-09-15 / 質問 #1265・#1266）
    実際には対応不要なメールが「本来当方で対応すべきメール」と書かれ、
    業務が止まっている前提で原因究明を進めた。最後に「個別の確認はしておらず、
    そのままアーカイブしている」と判明した。
    「困っていないが気になる」を書ける欄が無いと、報告するために事実が盛られる。
"""
import io
import os
import re

from lib.qa import posting_rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "pages", "2_💬_質問投稿.py")


def _page_source():
    return io.open(PAGE, encoding="utf-8").read()


def test_空欄を正直に書ける欄が消えていない():
    """■こちらで確認済み・■影響 は、否定や空欄をそのまま書くための欄。

    消すと、報告するために事実を盛るしかなくなる。消す変更はここで落とす。
    """
    headings = [head for head, _ in posting_rules.SECTIONS]
    for required in posting_rules.REQUIRED_HEADINGS:
        assert required in headings, "%s が SECTIONS から消えている" % required


def test_影響欄は止まっていない場合の書き方まで案内している():
    """「困っていない」と書いてよいことが本文に無いと、書く人は空欄か過大表現を選ぶ。"""
    desc = dict(posting_rules.SECTIONS)["■影響"]
    assert "止まっていない" in desc


def test_案内文にすべての見出しが載っている():
    text = posting_rules.rules_markdown()
    for head, _ in posting_rules.SECTIONS:
        assert head in text, "案内文に %s が無い" % head


def test_プレースホルダにすべての見出しが載っている():
    text = posting_rules.body_placeholder()
    for head, _ in posting_rules.SECTIONS:
        assert head in text, "プレースホルダに %s が無い" % head


def test_ページは正本から文言を生成している():
    src = _page_source()
    assert "from lib.qa import posting_rules" in src
    assert "posting_rules.rules_markdown()" in src
    assert "posting_rules.body_placeholder()" in src


def test_ページに記入ルールがベタ書きされていない():
    """同じ内容を2か所に書くと、片方だけ直して食い違う。復活したらここで落とす。"""
    src = _page_source()
    assert "記入ルール" not in src, "記入ルールの文言がページにベタ書きされている"
    assert not re.search(r'placeholder\s*=\s*["\']', src), \
        "プレースホルダがページに直書きされている（正本から生成すること）"
