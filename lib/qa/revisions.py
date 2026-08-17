# -*- coding: utf-8 -*-
"""
修正前の版を積む（回答修正・質問編集）。

方針は「上書きしない・消さない」。回答修正や質問編集で本文を書き換えるとき、
書き換え前の文章を 改訂履歴 プロパティへ日時つきで積んでおく。
画面では既定で見せない（読みたいときだけ開く）。

  push_revision(履歴, 種別, 本文, actor=..., at=...) … 1版積む
  parse_revisions(履歴)                              … 新しい順に取り出す

1版の書式:
    【回答修正前｜2026-08-17 10:30｜山田】
    （そのときの本文）
"""
import re

from lib.qa.history import now_jst

KIND_ANSWER = "回答修正前"
KIND_QUESTION = "質問修正前"

HEADER_RE = re.compile(
    r"^【(?P<kind>[^｜|】]+)(?:[｜|](?P<ts>[^｜|】]*))?(?:[｜|](?P<actor>[^】]*))?】[ 　]*$",
    re.M,
)
TS_FORMAT = "%Y-%m-%d %H:%M"


def push_revision(existing, kind, body, actor="", at=None):
    """改訂履歴に1版積む。新しい版が先頭に来る（開いたとき直近から読めるように）。

    body が空なら何もしない（初回の入力を「修正前の版」として積まないため）。
    """
    body = (body or "").strip()
    if not body:
        return existing or ""
    stamp = (at or now_jst()).strftime(TS_FORMAT)
    header = f"【{kind}｜{stamp}｜{actor or '不明'}】"
    entry = f"{header}\n{body}"
    existing = (existing or "").strip()
    return f"{entry}\n\n{existing}" if existing else entry


def parse_revisions(existing):
    """改訂履歴を [{"種別","日時","記録者","本文"}, ...] にして返す（積まれた順）。"""
    text = (existing or "").strip()
    if not text:
        return []
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        # 書式に合わないものは1件の塊として返す（読めなくして捨てるより良い）
        return [{"種別": "不明", "日時": "", "記録者": "", "本文": text}]
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append({
            "種別": (m.group("kind") or "").strip(),
            "日時": (m.group("ts") or "").strip(),
            "記録者": (m.group("actor") or "").strip(),
            "本文": text[m.end():end].strip(),
        })
    return out


def question_snapshot(title, body, tags):
    """質問編集の「修正前」を1本の文字列にまとめる。"""
    lines = [f"タイトル: {title or ''}"]
    if tags:
        lines.append(f"タグ: {'・'.join(tags)}")
    lines.append("本文:")
    lines.append((body or "").strip())
    return "\n".join(lines)
