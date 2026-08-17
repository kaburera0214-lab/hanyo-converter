# -*- coding: utf-8 -*-
"""
会話ログと回答本文の追記。

これまで追加回答を送ると 回答本文 を丸ごと上書きしていたため、最初の回答が
消えていた（2026-08時点で、ラリーした35件のうち34件で元の回答が失われていた）。
会話ログにも1900文字の上限があり、超えた分は先頭から捨てていた。

方針を「原則、全部残す」に変える。
  - 会話ログ  … 切らずに追記する（保存は lib/qa/notion_text が分割して全文入れる）
  - 回答本文  … 上書きせず、日時つきの見出しで追記する
  - 判断理由  … 詳細は追記、カテゴリは和集合

会話ログの1ターンの書式は thread_ui.MARKER_RE と揃える:
    【Q】… /【A】… /【追加Q｜YYYY-MM-DD HH:MM】… /【追加A｜YYYY-MM-DD HH:MM】…
"""
from lib.qa.history import now_jst

TS_FORMAT = "%Y-%m-%d %H:%M"


def _stamp(at=None):
    return (at or now_jst()).strftime(TS_FORMAT)


def start_log(question_body, answer_body):
    """会話ログが無い質問（ツール導入前のデータなど）から1往復ぶんを組み立てる。"""
    return f"【Q】{(question_body or '').strip()}\n【A】{(answer_body or '').strip()}"


def append_turn(log, kind, body, at=None):
    """会話ログに1ターン追記する。kind は "追加Q" か "追加A"。

    文字数で切らない。古い会話を捨てないのがこの関数の目的。
    """
    body = (body or "").strip()
    if not body:
        return log or ""
    turn = f"【{kind}｜{_stamp(at)}】{body}"
    log = (log or "").rstrip()
    return f"{log}\n\n{turn}" if log else turn


def append_answer(existing_answer, addition, at=None):
    """回答本文に追加回答を継ぎ足す。既存の回答は消さない。

    ここで作った文字列がAIドラフトの学習材料（質問本文とのペア）になるので、
    「その質問に対する回答の全体」がひとつながりで読める形にしておく。
    """
    addition = (addition or "").strip()
    if not addition:
        return existing_answer or ""
    existing = (existing_answer or "").rstrip()
    if not existing:
        return addition
    return f"{existing}\n\n【追加回答｜{_stamp(at)}】\n{addition}"


def append_reason(existing_detail, addition, at=None):
    """判断理由の詳細を継ぎ足す。空なら何もしない。"""
    addition = (addition or "").strip()
    if not addition:
        return existing_detail or ""
    existing = (existing_detail or "").rstrip()
    if not existing:
        return addition
    return f"{existing}\n\n【追加回答｜{_stamp(at)}】{addition}"


def merge_categories(existing, added):
    """判断理由カテゴリの和集合。既存の並びを保ったまま新しいものを後ろに足す。"""
    out = list(existing or [])
    for name in (added or []):
        if name not in out:
            out.append(name)
    return out
