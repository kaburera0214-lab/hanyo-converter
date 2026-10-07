#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
日次バッチ: 反映できなかった処理の控え（lib/pending_retry.py）が放置されていないかを見る。

入荷登録・価格改定で失敗した分は Drive に控えられ、人が「🔁 再実行」を押すまで
反映されない。押し忘れると、NEと楽天だけ新しい価格で Yahoo だけ旧いまま、という
食い違いが誰にも気づかれずに残る。だから滞留を見張る。

判定:
  控えが無い・全部済んでいる           → OK
  残っているが STALE_DAYS 未満         → OK（件数は出す）
  残って STALE_DAYS 以上               → 異常終了（GitHub Actionsを失敗させ、監視を赤にする）
  控えを読めない・日時を読めない       → 異常終了（「0件」に丸めない）

ここが失敗するのは処理エラーではなく「反映されないまま放置されている」の意味。

必要な環境変数（GitHub Secrets）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET  … Drive認証
  CHATWORK_API_TOKEN（任意）                                      … アラート
  APP_URL（任意）                                                 … 画面へのリンク
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

from batch import st_shim                          # noqa: E402
st_shim.install()                                  # libのimportより前に差し替える

from lib import pending_retry as pr                # noqa: E402

TAG = "[pending_retry_watch]"


def _alert(body):
    try:
        from lib.notify import chatwork
        if chatwork.is_configured():
            chatwork.create_task(body, limit_days=1, audience=chatwork.ADMIN)
    except Exception as e:  # noqa: BLE001（通知の失敗で判定結果を隠さない）
        print(f"{TAG} WARN: Chatwork通知に失敗: {e}", file=sys.stderr, flush=True)


def judge(entries, now=None):
    """控えの一覧から (終了コード, メッセージ, 詳細行) を決める（純関数・テスト対象）。"""
    opened = pr.open_of(entries)
    if not opened:
        return 0, "OK: 反映できていない処理はありません", []
    lines, ages = [], []
    for e in opened:
        age = pr.age_days(e, now)
        ages.append(age)
        place = pr.SOURCES.get(e.get("source"), e.get("source"))
        when = "日時不明" if age is None else f"{age}日前"
        lines.append(f"[{place}] {pr.created_jst(e)}（{when}）{e.get('label') or ''} "
                     + " / ".join(pr.describe(e.get("tasks"), limit=3)))
    if any(a is None for a in ages):
        return 1, f"NG: {len(opened)}件残っているが、控えた日時を読めないものがある", lines
    oldest = max(ages)
    if oldest >= pr.STALE_DAYS:
        return 1, f"NG: {len(opened)}件が最長{oldest}日間、反映されないまま残っている", lines
    return 0, (f"OK: {len(opened)}件が再実行待ち"
               f"（最古{oldest}日前・しきい値{pr.STALE_DAYS}日未満）"), lines


def main():
    entries = pr.load()              # 読めなければ StoreError（0件として扱わない）
    code, message, lines = judge(entries)
    print(f"{TAG} total={len(entries)} {message}", file=sys.stderr if code else sys.stdout,
          flush=True)
    for line in lines:
        print(f"{TAG}   {line}", flush=True)
    if code:
        app_url = (os.environ.get("APP_URL") or "").strip()
        link = f"\n画面: {app_url}" if app_url else ""
        _alert("[info][title]反映できていない処理が残っています[/title]"
               "入荷登録・価格改定で失敗した分が、再実行されないまま残っています。\n"
               "残っている間は、モールによって価格や送料設定が食い違ったままです。\n"
               "「📥 入荷登録」または「💰 価格改定」を開き、上のほうの"
               "「⏳ 反映できていない処理」で「🔁 再実行」を押してください。\n"
               + "\n".join(lines[:10]) + link + "[/info]")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"{TAG} FAILED: {e}", file=sys.stderr, flush=True)
        _alert("[info][title]反映できていない処理の点検が失敗しました[/title]"
               f"{e}\n控えを確認できていないので、残っていても気づけない状態です。[/info]")
        sys.exit(1)
