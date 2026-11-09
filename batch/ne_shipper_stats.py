#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ネクストエンジンの受注伝票から「誰が・いつ・何件 出荷確定したか」を集計してドライブへ出す。

【なぜ要るか】（2026-11-09）
  人ごとの出荷作業の時間を、実際に出荷確定した件数から出すため。出荷記録シートは
  スポットで手伝っただけでも全時間に数えてしまう。

【出荷確定者の項目は NE に無い】
  probe（2026-11-09）で receive_order_send_by_* は存在しなかった。代わりに
  「最終更新者」と「担当者」を出す。出荷確定のあとに伝票が触られていなければ、
  最終更新者＝出荷確定した人。その目安として、最終更新が出荷確定日時から
  CLOSE_MIN 分以内の件数を別の列に出す（この列が件数に近いほど、最終更新者で読んでよい）。

【このリポジトリは公開】
  担当者名・件数はログに出さない。出力先は Google ドライブ（非公開）だけ。
  ログには期間・伝票の総数・呼び出し回数・保存名だけを出す。

実行:
    python batch/ne_shipper_stats.py --from 2026-09-01 --to 2026-11-09
"""
import argparse
import csv
import datetime
import io
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                              # noqa: E402
st_shim.install()

from lib import master_store                           # noqa: E402
from lib.invoice import drive_master                   # noqa: E402
from lib.ne_api import client, usage                   # noqa: E402

BASE_EP = "api_v1_receiveorder_base/search"
CALL_BUDGET = 20
PAGE_LIMIT = 10000
CLOSE_MIN = 10
PREFIX = "ne_shipper_stats"

FIELDS = [
    "receive_order_id",
    "receive_order_shop_id",
    "receive_order_send_date",
    "receive_order_last_modified_date",
    "receive_order_last_modified_by_id",
    "receive_order_last_modified_by_name",
    "receive_order_pic_id",
    "receive_order_pic_name",
    "receive_order_delivery_name",
    "receive_order_statement_delivery_printing_date",
    "receive_order_cancel_type_id",
]


def _dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s[:19], fmt)
        except ValueError:
            pass
    return None


def fetch(date_from, date_to):
    filters = {"receive_order_send_date-gte": date_from + " 00:00:00",
               "receive_order_send_date-lte": date_to + " 23:59:59"}
    rows, offset, calls = [], 0, 0
    while True:
        if calls >= CALL_BUDGET:
            raise RuntimeError("NE APIの呼び出しが上限（{}回）に達しました".format(CALL_BUDGET))
        params = {"fields": ",".join(FIELDS), "limit": str(PAGE_LIMIT), "offset": str(offset)}
        params.update(filters)
        data = client.call(BASE_EP, params).get("data") or []
        calls += 1
        rows.extend(data)
        print("[fetch] offset={} → 累計{}件・呼び出し{}回".format(offset, len(rows), calls), flush=True)
        if len(data) < PAGE_LIMIT:
            return rows, calls
        offset += PAGE_LIMIT


def aggregate(rows):
    """(出荷確定日, 最終更新者, 担当者, 発送方法) ごとの件数と時刻の幅。"""
    agg = defaultdict(lambda: {"n": 0, "close": 0, "printed": 0, "times": []})
    for r in rows:
        send = _dt(r.get("receive_order_send_date"))
        if send is None:
            continue
        mod = _dt(r.get("receive_order_last_modified_date"))
        key = (send.strftime("%Y-%m-%d"),
               r.get("receive_order_last_modified_by_name") or "",
               r.get("receive_order_pic_name") or "",
               r.get("receive_order_delivery_name") or "")
        a = agg[key]
        a["n"] += 1
        if mod is not None and abs((mod - send).total_seconds()) <= CLOSE_MIN * 60:
            a["close"] += 1
        if r.get("receive_order_statement_delivery_printing_date"):
            a["printed"] += 1
        a["times"].append(send)
    return agg


def to_csv(agg):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["出荷確定日", "最終更新者", "担当者", "発送方法", "件数",
                "最終更新が出荷確定から{}分以内".format(CLOSE_MIN), "納品書印刷あり",
                "出荷確定の最初", "出荷確定の最後", "出荷確定の時刻の種類数"])
    for key in sorted(agg):
        a = agg[key]
        t = sorted(a["times"])
        w.writerow(list(key) + [a["n"], a["close"], a["printed"],
                                t[0].strftime("%H:%M:%S"), t[-1].strftime("%H:%M:%S"),
                                len({x.strftime("%H:%M") for x in t})])
    return buf.getvalue().encode("utf-8-sig")


def main():
    p = argparse.ArgumentParser(description="NE 出荷確定の人別集計")
    p.add_argument("--from", dest="date_from", required=True)
    p.add_argument("--to", dest="date_to", default="")
    args = p.parse_args()
    date_to = args.date_to or datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        rows, calls = fetch(args.date_from, date_to)
        if not rows:
            # 期間を指定して0件は「取れていない」とみなす
            print("[ne_shipper_stats] 伝票が1件も返りませんでした。保存しません。", file=sys.stderr, flush=True)
            return 1
        agg = aggregate(rows)
        name = drive_master.upload_versioned(to_csv(agg), PREFIX, master_store.folder_id())
        print("[ne_shipper_stats] OK saved={} 出荷確定日 {}〜{}・伝票{}件・集計{}行・呼び出し{}回".format(
            name, args.date_from, date_to, len(rows), len(agg), calls), flush=True)
        return 0
    finally:
        usage.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("[ne_shipper_stats] FAILED: {}".format(type(exc).__name__), file=sys.stderr, flush=True)
        print("[ne_shipper_stats] {}".format(str(exc)[:200]), file=sys.stderr, flush=True)
        sys.exit(1)
