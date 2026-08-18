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


def _alert(body, audience):
    """audience: chatwork.STAFF（現場が直せる）/ chatwork.ADMIN（開発者しか直せない）。"""
    try:
        from lib.notify import chatwork
        return chatwork.create_task(body, limit_days=1, audience=audience)
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
        from lib.notify import chatwork, ne_alerts
        if r.get("auth"):
            # 失効の復旧はブラウザでのログインだけ＝現場スタッフが自分でできる
            _alert(ne_alerts.reauth_body(APP_URL), chatwork.STAFF)
        else:
            # バッチ側の不具合。スタッフには直せないので管理者にだけ送る
            _alert(ne_alerts.admin_body(
                title="NEトークン延命バッチが失敗",
                error=r["message"],
                impact="このまま3日続くとNEの認証が切れ、入荷登録のNE自動反映が止まります。",
                action="ログを確認して修正 → Run workflow で再実行",
                workflow="ne-keepalive.yml"), chatwork.ADMIN)
    return 1


if __name__ == "__main__":
    sys.exit(main())
