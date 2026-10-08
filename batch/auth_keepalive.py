#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
毎日バッチ: 各接続先の認可が切れないよう見張る（ネクストエンジン・Yahoo）。

  NE    … 3日で失効。呼ぶたびに巻き直るので、ここを毎日実行していれば切れない
  Yahoo … 認可した日から28日で必ず失効し、延ばせない。期限の7日前から再認可を依頼し、
          3日前を切ったらこのバッチを失敗にする（稼働監視が赤になる）
経緯と根拠は lib/auth_keepalive.py と lib/yahoo_api/client.py の冒頭。

GitHub Actions（.github/workflows/auth-keepalive.yml）から実行する。
2026-08-31 に batch/ne_keepalive.py（NE専用）から移行。

必要な環境変数（GitHub Secrets）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET … Drive認証（トークンの保管先）
  NE_CLIENT_ID / NE_CLIENT_SECRET                              … NE認証
  YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET                        … Yahoo認証
  CHATWORK_API_TOKEN（任意）                                   … 失効時のアラート
  APP_URL（任意）                                              … 再認可ページへの直リンク用

※ Yahooが未設定（YAHOO_CLIENT_ID等が空）なら失敗にする（NEの分は実行される）。
   2026-10-07 まではスキップして成功にしており、Yahooを一度も見ていなかった。
※ 既に失効している場合はブラウザでの再認可が必要（API仕様。自動化できない）。
   その場合はChatworkにタスクを作って知らせる。

終了コード: 0=全て正常 / 1=いずれか失敗（認証切れ・期限間近・未設定・不具合）
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
# 再認可の期限の控え。ワークフローがリポジトリへコミットし、業務デスク（work-desk）が読む。
STATUS_PATH = os.path.join(ROOT, "data", "auth_status.json")


def _write_status(results):
    """期限の控えを書く。中身が同じなら触らない（＝コミットも増えない）。"""
    import json
    previous = {}
    try:
        with open(STATUS_PATH, encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        pass
    status = auth_keepalive.deadline_status(results, previous)
    if status == previous:
        return False
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return True


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

    if result.get("expiring"):
        # まだ切れていない。切れる前に再認可してもらえば、業務は一度も止まらない。
        audience = (chatwork.ADMIN if result.get("reauth_audience") == "admin"
                    else chatwork.STAFF)
        _alert(auth_alerts.expiring_body(result["key"], result.get("deadline_text", ""),
                                         APP_URL), audience)
        return

    if result.get("auth"):
        # 失効の復旧はブラウザでのログインだけ。ただし「誰のIDでログインするか」は
        # 接続先で違う。NEは倉庫スタッフのIDで完結するが、Yahooは店舗オーナーの
        # Yahoo IDが要るので現場に投げても動けない＝管理者へ送る。
        audience = (chatwork.ADMIN if result.get("reauth_audience") == "admin"
                    else chatwork.STAFF)
        _alert(auth_alerts.reauth_body(result["key"], APP_URL), audience)
        return

    # バッチ側の不具合。スタッフには直せないので管理者にだけ送る
    _alert(auth_alerts.admin_body(
        title=f"{label}の認可の見張りが失敗",
        error=result["message"],
        impact=(f"{label}の認証が切れても事前に気づけません。切れると"
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
        elif r.get("ok") and r.get("expiring"):
            print(line + f"WARN {r['message']}", flush=True)
        elif r.get("ok"):
            print(line + f"OK {r['message']}（rotated={r.get('rotated')}）", flush=True)
        else:
            kind = ("AUTH_ERROR" if r.get("auth")
                    else "EXPIRING" if r.get("expiring") else "FAILED")
            print(line + f"{kind}: {r['message']}", file=sys.stderr, flush=True)

    s = auth_keepalive.summarize(results)
    print(f"[auth_keepalive] 正常{s['ok']} / スキップ{s['skipped']} / "
          f"認証切れ{s['auth_error']} / 期限間近{s['expiring']} / 失敗{s['error']}",
          flush=True)

    try:
        if _write_status(results):
            print("[auth_keepalive] 再認可の期限の控えを更新しました（data/auth_status.json）",
                  flush=True)
    except Exception as e:  # noqa: BLE001 - 控えの失敗で見張り自体を止めない
        print(f"[auth_keepalive] WARN: 期限の控えを書けませんでした: {e}",
              file=sys.stderr, flush=True)

    # 通知は最後にまとめて出す。1件の通知失敗で他の延命結果を失わないため。
    for r in results:
        if not r.get("ok") and r.get("alert"):
            _notify(r)

    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
