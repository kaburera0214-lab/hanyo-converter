# -*- coding: utf-8 -*-
"""Claude API 呼び出しの共通口（lib/qa/ai.py）を検証する。

背景（2026-09-28）
    質問投稿の「新規質問として投稿する」で anthropic.BadRequestError が素通りし、
    トレースバックが出て投稿できなくなった。同じページのタグ整合チェックと
    類似質問の絞り込みは例外を握りつぶしていて、AIが止まっていることが見えなかった。

守りたいこと
    - ページが Claude API を直接呼ばない（呼ぶと例外の扱いがページごとにばらける）
    - 失敗の理由が利用者に読める日本語になる
    - キー未設定を「何もしない」に丸めず、AIUnavailable で上げる
"""
import glob
import os
import re

import pytest

from lib.qa import ai

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeAPIError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def test_pages_never_call_claude_directly():
    offenders = []
    for path in glob.glob(os.path.join(ROOT, "pages", "*.py")):
        src = open(path, encoding="utf-8").read()
        if re.search(r"\.messages\.create\(|^\s*import anthropic|^\s*from anthropic", src, re.M):
            offenders.append(os.path.basename(path))
    assert not offenders, f"lib/qa/ai.py の ask() を通してください: {offenders}"


@pytest.mark.parametrize("status,message,expected", [
    (400, "You have reached your specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.", "月間利用上限"),
    (400, "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.", "残高"),
    (401, "invalid x-api-key", "キーが無効"),
    (401, "API key is invalid.", "認証"),
    (429, "Number of request tokens has exceeded your rate limit", "利用上限"),
    (529, "Overloaded", "混雑"),
    (404, "model: claude-foo", "モデル"),
])
def test_describe_error_is_readable(status, message, expected):
    reason, detail = ai.describe_error(_FakeAPIError(status, message))
    assert expected in reason
    assert str(status) in detail and message in detail


def test_unknown_error_keeps_original_message():
    # 当てはまらない失敗を、それらしい理由に丸めない
    reason, _ = ai.describe_error(_FakeAPIError(400, "messages: text content blocks must be non-empty"))
    assert "non-empty" in reason and "400" in reason


def test_missing_key_raises_instead_of_silently_passing():
    with pytest.raises(ai.AIUnavailable) as ei:
        ai.ask("", "hi")
    assert "キー" in ei.value.reason
