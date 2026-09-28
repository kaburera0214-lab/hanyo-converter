# -*- coding: utf-8 -*-
"""Claude API 呼び出しの共通口（質問・回答管理の各ページが使う）。

経緯（2026-09-28）
    質問投稿の「新規質問として投稿する」で anthropic.BadRequestError が素通りし、
    画面にトレースバックが出て投稿できなくなった（Chatwork 趙娜さん報告）。
    同じページのタグ整合チェック・類似質問の絞り込みも同じAPIを呼んでいたが、
    こちらは例外を握りつぶしていたため「AIが止まっている」ことに誰も気づけなかった。

決まり
    - ページから client.messages.create を直接呼ばない。ここを通す
      （tests/test_qa_ai.py が pages/ 内の直接呼び出しを見つけたら落ちる）
    - 失敗は AIUnavailable に正規化し、利用者に読める理由（reason）を付ける
    - 失敗を「OK」や「該当なし」に丸めない。呼び出し側は必ず「AIを使えなかった」と表示する
"""

import sys

# 400/401/403/429/5xx で返る英語メッセージから、利用者向けの理由を決める。
# 上から順に見る。当てはまらなければ status と原文をそのまま出す（推測で丸めない）。
_REASONS = [
    # 2026-09-28 の質問投稿の停止はこれ（同日、子育て支援記録のスキャン取込も同じ400で止まっていた）。
    # 組織に設定した月間の利用上限。月が替わると解除されるので、残高不足とは対処が違う
    ("usage limits", "Claude API の月間利用上限に達しています。管理者が Anthropic Console で上限を引き上げるまで（または月初まで）AI機能は使えません"),
    ("credit balance", "Claude API の残高（クレジット）が不足しています。管理者がチャージするまでAI機能は使えません"),
    ("invalid x-api-key", "Claude API のキーが無効です。管理者がキーを確認する必要があります"),
    ("authentication", "Claude API の認証に失敗しました。管理者がキーを確認する必要があります"),
    ("rate limit", "Claude API の利用上限に一時的に達しました。数分おいて再度お試しください"),
    ("overloaded", "Claude API が混雑しています。数分おいて再度お試しください"),
    ("model", "指定したAIモデルが使えません。管理者がモデル名を確認する必要があります"),
]


def _reason_for(key):
    return dict(_REASONS)[key]


class AIUnavailable(Exception):
    """AIを使えなかった。reason は画面にそのまま出せる日本語。"""

    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def describe_error(exc):
    """例外 → (利用者向けの理由, ログ用の詳細)。anthropic を import しなくても判定できる。"""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        message = str((body.get("error") or {}).get("message") or "")
    if not message:
        message = str(exc)
    detail = f"{type(exc).__name__} status={status} message={message}"

    if not status and type(exc).__name__ in ("APIConnectionError", "APITimeoutError"):
        return "Claude API に接続できませんでした。数分おいて再度お試しください", detail
    lowered = message.lower()
    for key, reason in _REASONS:
        if key in lowered:
            return reason, detail
    if status == 401:
        return _reason_for("authentication"), detail
    if status == 429:
        return _reason_for("rate limit"), detail
    if status and status >= 500:
        return _reason_for("overloaded"), detail
    return f"AIの呼び出しに失敗しました（{type(exc).__name__} {status or ''}: {message[:200]}）", detail


def ask(api_key, prompt, *, model="claude-haiku-4-5", max_tokens=1024):
    """1問1答で本文テキストを返す。失敗は必ず AIUnavailable で上げる。"""
    if not api_key:
        raise AIUnavailable("Claude API のキーが設定されていません（管理者の設定待ち）", "ANTHROPIC_API_KEY is empty")
    import anthropic

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001 - 種類を問わず理由を付けて上げ直す
        reason, detail = describe_error(e)
        # Streamlit Cloud の「Manage app」のログに原文を残す（画面では伏せ字になるため）
        print(f"[qa.ai] {detail}", file=sys.stderr, flush=True)
        raise AIUnavailable(reason, detail) from e
    texts = [b.text for b in message.content if getattr(b, "type", "") == "text"]
    if not texts:
        raise AIUnavailable("AIの応答が空でした。もう一度お試しください", f"stop_reason={message.stop_reason}")
    return "".join(texts).strip()
