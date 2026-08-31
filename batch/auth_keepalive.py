#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
毎日バッチ: 各接続先の認可を延命する（ネクストエンジン・Yahoo）。

トークンは一定期間使わないと失効し、ブラウザでの再認可が必要になる
（NE=3日／Yahoo=28日）。価格改定も入荷登録も「都度」の不定期利用なので、
放っておけば必ず空白ができて、使いたいときに止まる。
毎日1回ここを実行しておけばトークンが更新され続け、再認可はほぼ不要になる。

GitHub Actions（.github/workflows/auth-keepalive.yml）から実行する。
2026-08-31 に batch/ne_keepalive.py（NE専用）から移行。

必要な環境変数（GitHub Secrets）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET … Drive認証（トークンの保管先）
  NE_CLIENT_ID / NE_CLIENT_SECRET                              … NE認証
  YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET                        … Yahoo認証
  CHATWORK_API_TOKEN（任意）                                   … 失効時のアラート
  APP_URL（任意）                                              … 再認可ページへの直リンク用

※ Yahooが未設定（YAHOO_CLIENT_ID等が空）なら、その分はスキップして続行する。
※ 既に失効している場合はブラウザでの再認可が必要（API仕様。自動化できない）。
   その場合はChatworkにタスクを作って知らせる。

終了コード: 0=全て正常（スキップ含む） / 1=いずれか失敗
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from batch import st_shim                              # noqa: E402
st_shim.install()                                      # libのimportより前に差し替える

from lib import auth_keepalive                         # noqa: E402

APP_URL = os.environ.get("APP_URL", "").strip()
WORKFLOW = "auth-keepalive.yml"


def _alert(body, audience):
    """audience: chatwork.STAFF（現場が直せる）/ chatwork.ADMIN（開発者しか直せない）。"""
    try:
        from lib.notify import chatwork
        return chatwork.create_task(body, limit_days=1, audience=audience)
    except Exception:  # noqa: BLE001 - 通知の失敗で延命自体を止めない
        return False


def _notify(result):
    """1件の失敗を、直せる人に向けて通知する。"""
    from lib.notify import auth_alerts, chatwork
    label = result["label"]

    if result.get("auth"):
        # 失効の復旧はブラウザでのログインだけ＝現場スタッフが自分でできる
        _alert(auth_alerts.reauth_body(result["key"], APP_URL), chatwork.STAFF)
        return

    # バッチ側の不具合。スタッフには直せないので管理者にだけ送る
    _alert(auth_alerts.admin_body(
        title=f"{label}のトークン延命バッチが失敗",
        error=result["message"],
        impact=(f"このまま放置すると{label}の認証が切れ、"
                f"価格改定・入荷登録の{label}への自動反映が止まります。"),
        action="ログを確認して修正 → Run workflow で再実行",
        workflow=WORKFLOW), chatwork.ADMIN)


def main():
    results = auth_keepalive.run_all()

    # NEのAPI使用量カウンタへ反映（NEを叩いた場合のみ意味がある）
    try:
        from lib.ne_api import usage
        usage.flush()
    except Exception:  # noqa: BLE001
        pass

    for r in results:
        line = f"[auth_keepalive] {r['label']}: "
        if r.get("skipped"):
            print(line + f"SKIP {r['message']}", flush=True)
        elif r.get("ok"):
            print(line + f"OK {r['message']}（rotated={r.get('rotated')}）", flush=True)
        else:
            kind = "AUTH_ERROR" if r.get("auth") else "FAILED"
            print(line + f"{kind}: {r['message']}", file=sys.stderr, flush=True)

    s = auth_keepalive.summarize(results)
    print(f"[auth_keepalive] 正常{s['ok']} / スキップ{s['skipped']} / "
          f"認証切れ{s['auth_error']} / 失敗{s['error']}", flush=True)

    # 通知は最後にまとめて出す。1件の通知失敗で他の延命結果を失わないため。
    for r in results:
        if not r.get("ok") and r.get("alert"):
            _notify(r)

    return 0 if (s["auth_error"] == 0 and s["error"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
