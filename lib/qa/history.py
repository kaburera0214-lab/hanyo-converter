# -*- coding: utf-8 -*-
"""
質問・回答管理の「編集履歴」プロパティの読み書き。

回答管理ページ（pages/3）はインハナさん・パピーの両方が使うため、
操作した人をログインで判別できない。運用上どのアクションを誰が行うかは
決まっているので、アクション種別から記録者を決め打ちする。

    ・質問投稿／質問編集／追加質問／完了／完了取消 … インハナ
    ・回答／追加回答                              … パピー
    ・回答修正                                    … パスワード認証した本人

履歴の1行フォーマット:  [YYYY-MM-DD HH:MM] 記録者：アクション
"""
import re
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

INHANA = "インハナ"
PUPPY = "パピー"

ACTOR_BY_ACTION = {
    "質問投稿": INHANA,
    "質問編集": INHANA,
    "追加質問": INHANA,
    "完了": INHANA,
    "完了取消": INHANA,
    "回答": PUPPY,
    "追加回答": PUPPY,
    # 「回答修正」は認証した本人の名前を使うので、ここには入れない
}

HISTORY_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]*)\]\s*(?P<actor>[^：:]+)[：:]\s*(?P<action>.+?)\s*$")

MAX_LINES = 30
MAX_CHARS = 1900  # Notionのrich_textは2000文字上限

# 「完了を取り消す」を出しておく時間。誤操作のリカバリ用なので短くする。
# ナレッジとして確定させるため、これを過ぎた質問は完了のまま戻せない。
UNDO_WINDOW_HOURS = 3


def now_jst():
    return datetime.now(JST)


def actor_for(action):
    """アクションから記録者を返す。未知のアクションは空文字。"""
    return ACTOR_BY_ACTION.get(action, "")


def history_entry(action, actor=None, at=None):
    timestamp = (at or now_jst()).strftime("%Y-%m-%d %H:%M")
    return f"[{timestamp}] {actor or actor_for(action)}：{action}"


def trim_history(lines):
    """行数・文字数の上限に収める（古い行から捨てる）。"""
    lines = list(lines)[-MAX_LINES:]
    result = "\n".join(lines)
    while len(result) > MAX_CHARS and len(lines) > 1:
        lines = lines[1:]
        result = "\n".join(lines)
    return result


def append_history(existing_history, action, actor=None, at=None):
    """履歴に1行追記する。記録者は原則アクションから自動判定。

    actor を明示するのは「回答修正」のようにログイン名で残したいときだけ。
    """
    existing = (existing_history or "").strip()
    lines = existing.split("\n") if existing else []
    lines.append(history_entry(action, actor=actor, at=at))
    return trim_history(lines)


def last_action_at(existing_history, action):
    """履歴から指定アクションの最後の日時を返す。無ければ None。"""
    found = None
    for line in (existing_history or "").split("\n"):
        m = HISTORY_LINE_RE.match(line)
        if not m or m.group("action") != action:
            continue
        try:
            found = datetime.strptime(m.group("ts").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            continue
    return found


def undo_remaining(existing_history, action="完了", hours=UNDO_WINDOW_HOURS, now=None):
    """取消ボタンを出しておく残り時間を返す。取消不可なら None。

    履歴に該当アクションが無い（＝履歴が残る前の古いデータ）場合も
    期限切れ扱いにして None を返す。
    """
    at = last_action_at(existing_history, action)
    if at is None:
        return None
    remaining = (at + timedelta(hours=hours)) - (now or now_jst())
    return remaining if remaining.total_seconds() > 0 else None


def retag_history(existing_history):
    """既存の履歴の記録者を ACTOR_BY_ACTION に合わせて直す（過去分の遡及修正用）。

    戻り値: (修正後の履歴, 変更した行数)
    マップに無いアクション（回答修正など）や、書式に合わない行は触らない。
    """
    existing = (existing_history or "").strip()
    if not existing:
        return existing_history or "", 0

    changed = 0
    fixed = []
    for line in existing.split("\n"):
        m = HISTORY_LINE_RE.match(line)
        if not m:
            fixed.append(line)
            continue
        expected = ACTOR_BY_ACTION.get(m.group("action"))
        if expected and expected != m.group("actor").strip():
            fixed.append(f"[{m.group('ts')}] {expected}：{m.group('action')}")
            changed += 1
        else:
            fixed.append(line)

    return trim_history(fixed), changed
