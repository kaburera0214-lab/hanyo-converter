#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
日次バッチ: Yahoo配送グループが本当に反映されたかを確認する。

入荷登録は配送グループを uploadItemFile（項目指定）で送るが、**反映は非同期**なので
「送った」＝「反映された」ではない。送った分は Drive のキュー（反映確認待ち）に
控えてあるので、ここで getItem を読み、あるべき配送グループNoになっていれば
キューから外す。残ったものが本当の未反映。

判定:
  キューが空                        → OK
  今回の点検で全部が一致して外れた  → OK
  残ったが最古が STALE_DAYS 未満    → OK（件数は出す）
  残って最古が STALE_DAYS 以上      → 異常終了（GitHub Actionsを失敗させ、監視を赤にする）
  現在値を読めなかった              → 「反映済み」に丸めず未確認のまま残す

ここが失敗するのは処理エラーではなく「Yahooへ反映されないまま放置されている」の意味。
主な原因は、商品がYahoo未登録、プロダクトカテゴリ未設定などでアップロードが弾かれること。

必要な環境変数（GitHub Secrets / ローカルのenv）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET  … Drive認証
  YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_SELLER_ID         … Yahoo認証
  PRODUCT_MASTER_FOLDER_ID                                        … キューCSVのフォルダ
  CHATWORK_API_TOKEN（任意）                                      … 未反映アラート
  APP_URL（任意）                                                 … 入荷登録画面へのリンク
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                          # noqa: E402
st_shim.install()                                  # libのimportより前に差し替える

from lib import mall_routes, master_store          # noqa: E402
from lib.pricing import masters                    # noqa: E402
from lib.receiving import yahoo_queue as yq        # noqa: E402
from lib.yahoo_api import items as yitems          # noqa: E402

TAG = "[yahoo_queue_watch]"


def _alert(body):
    try:
        from lib.notify import chatwork
        if chatwork.is_configured():
            chatwork.create_task(body, limit_days=1, audience=chatwork.ADMIN)
    except Exception as e:  # noqa: BLE001（通知の失敗で判定結果を隠さない）
        print(f"{TAG} WARN: Chatwork通知に失敗: {e}", file=sys.stderr, flush=True)


def _group_no(folder):
    settings = masters.load_settings(folder)
    mall_routes.seed_yahoo_group_no(settings)      # 未設定なら確定値で補う（保存はしない）
    return {bin_name: str(settings.get(key, "")).strip()
            for bin_name, key in mall_routes.YAHOO_GROUP_SETTING_KEY.items()}


def _reflected_codes(df, group_no):
    """反映が確認できたコードと、確認できなかった行の説明を返す。"""
    current = yitems.get_postage_sets(list(df["code"]))
    done, pending = [], []
    for _, row in df.iterrows():
        code = str(row["code"]).strip()
        want = yq.expected_no(row, group_no)
        info = current.get(code) or {"state": yitems.STATE_ERROR, "value": None}
        if info["state"] == yitems.STATE_OK and want and str(info["value"]).strip() == want:
            done.append(code)
        elif info["state"] == yitems.STATE_NOT_FOUND:
            pending.append(f"{code}: Yahoo未登録（商品を登録するまで反映できません）")
        elif info["state"] != yitems.STATE_OK:
            pending.append(f"{code}: 現在値を読めず未確認")
        else:
            pending.append(f"{code}: 現在No.{info['value'] or '未設定'} / あるべきNo.{want or '不明'}")
    return done, pending


def main():
    folder = master_store.folder_id()
    df = yq.load_delivery(folder)
    print(f"{TAG} folder={folder} pending={len(df)}", flush=True)
    if len(df) == 0:
        print(f"{TAG} OK: 反映確認待ちなし", flush=True)
        return 0

    app_url = (os.environ.get("APP_URL") or "").strip()
    link = f"\n画面: {app_url}" if app_url else ""

    done, pending = _reflected_codes(df, _group_no(folder))
    if done:
        n = yq.resolve_delivery(done, folder)
        print(f"{TAG} 反映を確認: {n}件（{'、'.join(done[:10])}）", flush=True)
        df = yq.load_delivery(folder)

    if len(df) == 0:
        print(f"{TAG} OK: すべて反映を確認しました", flush=True)
        return 0

    age = yq.oldest_age_days(df)
    detail = "\n".join(pending[:10])
    if age is None:
        print(f"{TAG} NG: {len(df)}件残っているが追加日時を読めない", file=sys.stderr, flush=True)
        _alert(f"[info][title]Yahoo配送グループ: 滞留期間を判定できません[/title]"
               f"{len(df)}件が未反映ですが、追加日時が読めず期間を判定できません。\n"
               f"{detail}{link}[/info]")
        return 1

    print(f"{TAG} pending={len(df)} oldest={age}日前 threshold={yq.STALE_DAYS}日", flush=True)
    if age >= yq.STALE_DAYS:
        print(f"{TAG} NG: {len(df)}件が最長{age}日間、Yahooへ未反映\n{detail}",
              file=sys.stderr, flush=True)
        _alert(f"[info][title]Yahoo配送グループが{age}日間ずっと未反映です（{len(df)}件）[/title]"
               f"NEと楽天は便種が変わっているのに、Yahooの送料設定だけ旧のままです。\n"
               f"アップロードが弾かれている可能性があります（Yahoo未登録・"
               f"プロダクトカテゴリ未設定など）。\n{detail}{link}[/info]")
        return 1

    print(f"{TAG} OK: {len(df)}件が反映待ち（最古{age}日前・しきい値{yq.STALE_DAYS}日未満）",
          flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"{TAG} FAILED: {e}", file=sys.stderr, flush=True)
        _alert(f"[info][title]Yahoo配送グループの点検が失敗しました[/title]"
               f"{e}\nキューを確認できていないので、未反映でも気づけない状態です。[/info]")
        sys.exit(1)
