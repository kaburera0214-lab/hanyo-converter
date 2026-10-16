#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ネクストエンジンの在庫入出庫履歴のうち「入庫」を取り出すバッチ。

【なぜ要るか】
  中国から仕入れた商品（さくら代行）を、NE の商品コードと確実に紐づけるため（2026-10-16 ユーザー案）。
  さくら代行の発送日から入荷の時期が分かるので、その時期に NE で入庫処理された商品コードと数量を
  突き合わせれば、どの仕入れがどの商品かが決まる。突き合わせは import-cost（ローカル）が行う。

【このリポジトリは公開】
  ログにも成果物にも入庫の中身（商品コード・数量・事由）を出さない。出力先は Google ドライブだけ。
  担当者名などの項目は取らない（突き合わせに要らない）。--probe は項目名だけを出す。

【NE APIの無料枠を守る】
  月1000回まで無料（lib/ne_api/usage.py）。1回で最大1万件取れるので、期間をまとめてページングを
  最小にする。CALL_BUDGET を超えたら異常終了する（途中までを全件として保存しない）。

【保存先】
  Google ドライブの商品マスタと同じフォルダに `ne_stock_in_YYYYMMDD_NNN.csv`（上書きしない・版を足す）。

実行:
    python batch/ne_stock_io.py --probe                          # 項目の確認だけ（API 1回）
    python batch/ne_stock_io.py --from 2019-01-01 --to 2026-10-16  # 入出庫日の範囲で全部
    python batch/ne_stock_io.py --days 7                         # 直近7日に作られた入庫
"""
import argparse
import csv
import datetime
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                              # noqa: E402
st_shim.install()                                      # libのimportより前に差し替える

from lib import master_store                           # noqa: E402
from lib.invoice import drive_master                   # noqa: E402
from lib.ne_api import client, usage                   # noqa: E402

EP = "api_v1_master_stockiohistory/search"
CALL_BUDGET = 40
PAGE_LIMIT = 10000
DRIVE_PREFIX = "ne_stock_in"
JST = datetime.timezone(datetime.timedelta(hours=9))

# 公式ドキュメント（developer.next-engine.com/api/api_v1_master_stockiohistory/search）の項目名。
# 担当者名・作成者名は取らない（突き合わせに要らず、個人に結びつくため）。
FIELDS = [
    "stock_io_history_id",
    "stock_io_history_goods_id",                 # 商品コード
    "stock_io_history_date",                     # 入出庫日
    "stock_io_history_quantity",                 # 入出庫数
    "stock_io_history_before_stock_quantity",    # 更新前在庫数
    "stock_io_history_after_stock_quantity",     # 更新後在庫数
    "stock_io_history_io_flag",                  # 0:入庫 1:出庫
    "stock_io_history_io_type_id",               # 入出庫区分
    "stock_io_history_io_type_name",             # 入出庫名
    "stock_io_history_reason",                   # 事由
    "stock_io_history_cut_form_id",              # 伝票番号（発注の入荷なら発注伝票）
    "stock_io_history_deleted_flag",
    "stock_io_history_creation_date",
]
BASE_FILTER = {"stock_io_history_io_flag-eq": "0", "stock_io_history_deleted_flag-eq": "0"}


def probe():
    """項目名が通るかだけ確かめる。値は出さない（公開ログのため）。"""
    res = client.call(EP, {"fields": ",".join(FIELDS), "limit": "1", **BASE_FILTER})
    rows = res.get("data") or []
    print("[probe] NE APIを 1 回呼びました。項目 {} 個はすべて通りました。".format(len(FIELDS)), flush=True)
    if rows:
        empty = [k for k, v in rows[0].items() if v in (None, "")]
        print("[probe] 1件目で空の項目: {}".format(", ".join(empty) or "なし"), flush=True)
    else:
        print("[probe] 入庫の履歴が1件も返りませんでした。", flush=True)
    return 0


def fetch(filters):
    rows, offset, calls = [], 0, 0
    while True:
        if calls >= CALL_BUDGET:
            raise RuntimeError("NE APIの呼び出しが上限（{}回）に達しました。{}件で中断します。"
                               "期間を分けて実行してください。".format(CALL_BUDGET, len(rows)))
        params = {"fields": ",".join(FIELDS), "limit": str(PAGE_LIMIT), "offset": str(offset)}
        params.update(BASE_FILTER)
        params.update(filters)
        data = client.call(EP, params).get("data") or []
        calls += 1
        rows.extend(data)
        print("[fetch] offset={} → {}件（累計{}件・呼び出し{}回）".format(offset, len(data), len(rows), calls), flush=True)
        if len(data) < PAGE_LIMIT:
            return rows, calls
        offset += PAGE_LIMIT


def to_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator=chr(13) + chr(10))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in FIELDS})
    return buf.getvalue().encode("utf-8-sig")


def export(args):
    if args.date_from:
        end = args.date_to or datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filters = {"stock_io_history_date-gte": args.date_from + " 00:00:00",
                   "stock_io_history_date-lte": end + " 23:59:59"}
        label, whole = "入出庫日 {}〜{}".format(args.date_from, end), True
    else:
        since = datetime.datetime.now(JST) - datetime.timedelta(days=args.days)
        filters = {"stock_io_history_creation_date-gte": since.strftime("%Y-%m-%d 00:00:00")}
        label, whole = "直近{}日に作られた入庫".format(args.days), False

    print("[ne_stock_io] {} を取ります".format(label), flush=True)
    rows, calls = fetch(filters)
    if not rows:
        if whole:
            print("[ne_stock_io] 入庫が1件も返りませんでした。保存しません。", file=sys.stderr, flush=True)
            return 1
        print("[ne_stock_io] 新しい入庫はありませんでした（保存なし）。", flush=True)
        return 0
    name = drive_master.upload_versioned(to_csv(rows), DRIVE_PREFIX, master_store.folder_id())
    print("[ne_stock_io] OK saved={} 入庫{}行・呼び出し{}回".format(name, len(rows), calls), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description="NE在庫入出庫履歴（入庫）の取り出し")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--from", dest="date_from", default="")
    parser.add_argument("--to", dest="date_to", default="")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    try:
        return probe() if args.probe else export(args)
    finally:
        usage.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("[ne_stock_io] FAILED: {}".format(type(exc).__name__), file=sys.stderr, flush=True)
        print("[ne_stock_io] {}".format(str(exc)[:200]), file=sys.stderr, flush=True)
        sys.exit(1)
