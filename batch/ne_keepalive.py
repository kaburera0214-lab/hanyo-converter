#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
毎日バッチ: NEのトークンを延命する（軽いAPIを1回呼ぶだけ）。

NEのrefresh_tokenは発行から3日で切れるため、入荷登録を数日使わないと必ず認証切れになる。
毎日1回これを実行しておけばトークンが更新され続け、再認可はほぼ不要になる。
GitHub Actions（.github/workflows/ne-keepalive.yml）から実行する。

必要な環境変数（GitHub Secrets）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET  … Drive認証
  NE_CLIENT_ID / NE_CLIENT_SECRET                                … NE認証
  CHATWORK_API_TOKEN（任意）                                     … 失効時のアラート

NE API消費は1日1回＝月30回（無料枠1000回/月）。
※既に3日以上放置して失効している場合は、ブラウザでの再認可が必要（NE仕様）。
  その場合はChatworkにタスクを作って知らせる。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                              # noqa: E402
st_shim.install()                                      # libのimportより前に差し替える

from lib.ne_api import keepalive, usage                # noqa: E402

APP_URL = os.environ.get("APP_URL", "").strip()


def _alert(body):
    try:
        from lib.notify import chatwork
        return chatwork.create_task(body, limit_days=1)
    except Exception:  # noqa: BLE001
        return False


def main():
    r = keepalive.run()
    try:
        usage.flush()      # この呼び出しもNE API使用量カウンタへ反映する
    except Exception:  # noqa: BLE001
        pass

    if r["ok"]:
        print(f"[ne_keepalive] OK {r['message']}"
              f"（rotated={r['rotated']}）", flush=True)
        return 0

    print(f"[ne_keepalive] FAILED: {r['message']}", file=sys.stderr, flush=True)
    if r.get("alert"):
        where = f"\n{APP_URL}" if APP_URL else ""
        if r.get("auth"):
            _alert("[info][title]🔐 ネクストエンジンの再認可が必要です[/title]"
                   "NEのトークンが失効しました（3日以上APIを呼べていません）。"
                   "このままだと入荷登録のNE自動更新が失敗します。\n"
                   "対応: パピー業務ツール →「📥 入荷登録」→「🔐 NE API接続」→"
                   "「🔑 NEにログインして認可する」を押してNEにログインするだけです"
                   f"（1分で終わります）。{where}[/info]")
        else:
            _alert("[info][title]⚠️ NEトークン延命バッチが失敗[/title]"
                   f"{r['message']}\n"
                   "3日以内に復旧しないとNEの認証が切れます。"
                   f"GitHub Actionsのログを確認してください。{where}[/info]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
