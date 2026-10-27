#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ネクストエンジンの発注明細（いつ・どの商品を・いくらで発注したか）を取り出すバッチ。

【なぜ要るか】
  GapBoard の原価は、受注伝票の「受注時の原価」（その時のNE商品マスタの値）で決まる。
  マスタの原価が実際の発注単価と違っていると、利益が実態とずれる
  （2026-10-27 三輪車iimo：マスタは8,580円、2024年9月の発注は7,000円とユーザー指摘）。
  発注単価と照らして、ずれている商品の一覧を作るために取る。

【このリポジトリは公開】
  Actions のログも成果物も誰でも見られる。発注の中身（商品・単価・仕入先）を
  ログに出さない／artifact に上げない。出力先は Google ドライブ（非公開）だけにする。

【NE APIの無料枠を守る】
  月1000回まで無料（lib/ne_api/usage.py）。1回で最大1万件取れる。CALL_BUDGET を超えたら
  異常終了する（途中までを全件として保存しない）。

【保存先】
  Google ドライブの商品マスタと同じフォルダに `ne_placeorder_rows_YYYYMMDD_NNN.csv`。
  GapBoard の毎朝の取り込み（ne_order_rows_*）とは名前を分けて混ぜない。上書きしない（版を足す）。

実行:
    python batch/ne_placeorder_rows.py                       # 発注明細を全期間
    python batch/ne_placeorder_rows.py --from 2024-01-01      # 作成日がこの日以降
"""
import argparse
import csv
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

ROW_EP = "api_v1_placeorder_row/search"
CALL_BUDGET = 30
PAGE_LIMIT = 10000
DRIVE_PREFIX = "ne_placeorder_rows"

# 項目名は NE の API 説明書（発注明細検索）から。1つでも存在しない名前が混ざると
# 呼び出し全体が失敗するので、弾かれた名前を外して投げ直す。
ROW_FIELDS = [
    "place_order_row_place_order_id",     # 発注伝票番号
    "place_order_row_no",                 # 発注明細行
    "place_order_row_goods_id",           # 商品コード
    "place_order_row_goods_name",         # 商品名
    "place_order_row_unit_price",         # 発注単価
    "place_order_row_place_quantity",     # 発注数
    "place_order_row_sub_total_price",    # 小計
    "place_order_row_supply_quantity",    # 仕入数
    "place_order_row_cancel_flag",        # キャンセル
    "place_order_row_completed_flag",     # 完了
    "place_order_row_type_name",          # 発注種別
    "place_order_row_receive_order_id",   # 受注伝票番号（直送など受注から作った発注）
    "place_order_row_creation_date",      # 作成日
]
BASE_FIELDS = [
    "place_order_date",                   # 発注日
    "place_order_supplier_id",            # 仕入先コード
    "place_order_status_name",            # 発注伝票の状態
    "place_order_cancel_flag",            # 発注伝票のキャンセル
]


def _call(fields, extra):
    params = {"fields": ",".join(fields), "limit": str(PAGE_LIMIT)}
    params.update(extra)
    return client.call(ROW_EP, params).get("data") or []


def usable_fields():
    """実在する項目だけに絞る。エラー文に名前が出た項目を外して投げ直す（値は出さない）。"""
    alive = ROW_FIELDS + BASE_FIELDS
    for _ in range(len(alive)):
        try:
            _call(alive, {"limit": "1"})
            return alive
        except Exception as exc:  # noqa: BLE001
            named = [f for f in alive if f in str(exc)]
            if not named:
                raise
            for f in named:
                alive.remove(f)
                print("[placeorder] 使えない項目を外しました: {}".format(f), flush=True)
    raise RuntimeError("使える項目が残りませんでした")


def fetch(fields, filters):
    rows, offset, calls = [], 0, 0
    while True:
        if calls >= CALL_BUDGET:
            raise RuntimeError("NE APIの呼び出しが上限（{}回）に達しました。{}件で中断します。"
                               .format(CALL_BUDGET, len(rows)))
        extra = {"offset": str(offset)}
        extra.update(filters)
        data = _call(fields, extra)
        calls += 1
        rows.extend(data)
        print("[placeorder] offset={} → {}件（累計{}件・呼び出し{}回）".format(
            offset, len(data), len(rows), calls), flush=True)
        if len(data) < PAGE_LIMIT:
            return rows
        offset += PAGE_LIMIT


def to_csv(rows, fields):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore",
                            lineterminator=chr(13) + chr(10))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})
    return buf.getvalue().encode("utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description="NE発注明細の取り出し")
    parser.add_argument("--from", dest="date_from", default="",
                        help="発注明細の作成日の開始（YYYY-MM-DD）。省略時は全期間")
    args = parser.parse_args()
    try:
        fields = usable_fields()
        filters = {}
        if args.date_from:
            filters["place_order_row_creation_date-gte"] = args.date_from + " 00:00:00"
        rows = fetch(fields, filters)
        if not rows:
            # 発注が1件も無いことはないので、取れていないと考える（0件を正常にしない）
            print("[placeorder] 発注明細が1件も返りませんでした。保存しません。", file=sys.stderr, flush=True)
            return 1
        name = drive_master.upload_versioned(to_csv(rows, fields), DRIVE_PREFIX, master_store.folder_id())
        print("[placeorder] OK saved={} 明細{}行・項目{}個".format(name, len(rows), len(fields)), flush=True)
        return 0
    finally:
        usage.flush()      # 失敗した回も、使ったAPI回数は数える


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # 例外の文面に発注の中身が混ざることがあるので、種類と先頭だけ出す
        print("[placeorder] FAILED: {}".format(type(exc).__name__), file=sys.stderr, flush=True)
        print("[placeorder] {}".format(str(exc)[:200]), file=sys.stderr, flush=True)
        sys.exit(1)
