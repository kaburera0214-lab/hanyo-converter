#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
日次バッチ: Yahoo配送グループ待機キューの滞留を見張る。

配送グループだけは Yahoo APIに部分更新の手段が無く（lib/mall_routes.py 参照）、
人がストアクリエイターProへCSVをアップするまで反映されない。
＝**忘れられたまま何ヶ月でも放置できてしまう唯一の経路**なので、ここだけは
外から見張る。滞留していたら非ゼロ終了し、GitHub Actionsを失敗させる
（稼働監視ダッシュボードが赤にする）。Chatworkにも管理者タスクを1本立てる。

判定:
  0件                → OK
  1件以上 / 最古が STALE_DAYS 未満 → OK（ただし件数は出す）
  1件以上 / 最古が STALE_DAYS 以上 → 異常終了
  日時が読めない     → 異常終了（「読めない」を「正常」に丸めない）

必要な環境変数（GitHub Secrets / ローカルのenv）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET  … Drive認証
  PRODUCT_MASTER_FOLDER_ID                                       … キューCSVのフォルダ
  CHATWORK_API_TOKEN（任意）                                     … 滞留アラート
  APP_URL（任意）                                                … 入荷登録画面へのリンク
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                          # noqa: E402
st_shim.install()                                  # libのimportより前に差し替える

from lib import master_store                       # noqa: E402
from lib.receiving import yahoo_queue as yq        # noqa: E402

TAG = "[yahoo_queue_watch]"


def _alert(body):
    try:
        from lib.notify import chatwork
        if chatwork.is_configured():
            chatwork.create_task(body, limit_days=1, audience=chatwork.ADMIN)
    except Exception as e:  # noqa: BLE001（通知の失敗で判定結果を隠さない）
        print(f"{TAG} WARN: Chatwork通知に失敗: {e}", file=sys.stderr, flush=True)


def main():
    folder = master_store.folder_id()
    df = yq.load_delivery(folder)
    n = len(df)
    print(f"{TAG} folder={folder} pending={n}", flush=True)

    if n == 0:
        print(f"{TAG} OK: 反映待ちなし", flush=True)
        return 0

    age = yq.oldest_age_days(df)
    codes = "、".join(str(c) for c in df.get("code", [])[:10])
    app_url = (os.environ.get("APP_URL") or "").strip()
    link = f"\n画面: {app_url}" if app_url else ""

    if age is None:
        print(f"{TAG} NG: {n}件あるが追加日時を読めない（滞留期間を判定できない）",
              file=sys.stderr, flush=True)
        _alert(f"[info][title]Yahoo配送グループ待機キュー: 判定できません[/title]"
               f"{n}件が反映待ちですが、追加日時が読めず滞留期間を判定できません。"
               f"キューCSVの形式を確認してください。\n対象: {codes}{link}[/info]")
        return 1

    print(f"{TAG} pending={n} oldest={age}日前 threshold={yq.STALE_DAYS}日", flush=True)
    if age >= yq.STALE_DAYS:
        print(f"{TAG} NG: {n}件が最長{age}日間、Yahooへ未反映", file=sys.stderr, flush=True)
        _alert(f"[info][title]Yahoo配送グループが{age}日間ずっと未反映です（{n}件）[/title]"
               f"NEと楽天は便種が変わっているのに、Yahooの送料設定だけ旧のままです。\n"
               f"入荷登録の「🟡 Yahoo配送グループ待機キュー」からCSVを落として、"
               f"ストアクリエイターProに『項目指定』でアップしてください。\n"
               f"対象: {codes}{link}[/info]")
        return 1

    print(f"{TAG} OK: {n}件（最古{age}日前・しきい値{yq.STALE_DAYS}日未満）", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"{TAG} FAILED: {e}", file=sys.stderr, flush=True)
        _alert(f"[info][title]Yahoo配送グループ待機キューの点検が失敗しました[/title]"
               f"{e}\nキューを確認できていないので、滞留していても気づけない状態です。[/info]")
        sys.exit(1)
