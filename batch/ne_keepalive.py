#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【廃止】2026-08-31。batch/auth_keepalive.py に統合しました。

このスクリプトはNE専用で、Yahooの認可を延命していなかった。
2026-08-31、価格改定でYahooが "refresh token has expired" で失敗し、
延命の対象がNEだけだったことが判明したため、接続先を問わない
batch/auth_keepalive.py（NE・Yahoo）へ作り直した。

呼び出していた .github/workflows/ne-keepalive.yml も
.github/workflows-retired/ へ退避済みなので、このファイルは実行されない。
経緯を残すために消していないだけで、**新しく参照しないこと**。
NEだけを延命したい場合も auth_keepalive.py を使う。
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
