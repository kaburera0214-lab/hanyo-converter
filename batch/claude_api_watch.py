#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
日次バッチ: 業務ツールのAI機能（Claude API）が使える状態かを確認する。

経緯（2026-09-28）
    質問投稿の「新規質問として投稿する」が anthropic.BadRequestError で止まり、
    外注スタッフからの報告で初めて分かった。AIの失敗は画面を開いた人にしか見えないので、
    残高切れ・キー失効を「誰かが押して止まるまで」気づけない。毎朝1回だけ最小の
    リクエストを送り、失敗したらこのジョブを落として稼働監視ダッシュボードを赤にする。

判定:
  応答が返った                     → OK
  キーが未設定（Secrets未登録）    → 異常終了（「確認できていない」を正常に丸めない）
  残高不足・認証失敗・その他       → 異常終了（理由はログに日本語で出す）

必要な環境変数（GitHub Secrets）:
  ANTHROPIC_API_KEY … Streamlit Cloud のアプリと同じ組織のキー
                      （残高は組織単位なので、同じ組織なら別キーでも判定できる）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.qa import ai  # noqa: E402

# 質問投稿・回答管理が使うモデル。ここが使えなければ画面も使えない
MODEL = "claude-haiku-4-5"


def main():
    try:
        text = ai.ask(os.environ.get("ANTHROPIC_API_KEY", ""), "OKとだけ返してください", model=MODEL, max_tokens=5)
    except ai.AIUnavailable as e:
        print(f"NG: {e.reason}\n詳細: {e.detail}")
        return 1
    print(f"OK: {MODEL} が応答しました（{text!r}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
