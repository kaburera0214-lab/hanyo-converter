#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ネクストエンジンの受注明細（伝票に確定している商品コードと数量）を取り出すバッチ。

【なぜ要るか】
  実際に何を発送したかの根拠はネクストエンジンの受注伝票にある。モールの注文データには
  出てこない行がある（例: 無料ラッピングを選ぶと、NEが商品マスタの項目4を見てラッピング袋を
  自動で明細に追加する）。原価は「モールで売れたSKU」ではなく「NEの伝票で確定している
  商品コード」で確定させる（2026-10-07 ユーザー決定）。

【このリポジトリは公開】
  Actions のログも成果物も誰でも見られる。受注データ（注文番号・商品・数量）を
  ログに出さない／artifact に上げない。出力先は Google ドライブ（非公開）だけにする。
  --probe が出すのは **項目名だけ**（値は出さない）。

【NE APIの無料枠を守る】
  月1000回まで無料（lib/ne_api/usage.py）。1回で最大1万件取れるので、期間をまとめて
  ページングを最小にする。CALL_BUDGET を超えたら異常終了する（途中までを全件として
  保存しない）。

実行:
    python batch/ne_order_rows.py --probe                 # 項目名の確認だけ（API 1回）
"""
import argparse
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                              # noqa: E402
st_shim.install()                                      # libのimportより前に差し替える

from lib.ne_api import client, usage                   # noqa: E402

ROW_EP = "api_v1_receiveorder_row/search"

CALL_BUDGET = 60         # このバッチ1回で使ってよい呼び出しの上限


# 項目名の候補。NE の受注明細検索は fields の指定が必須で（未指定だと 004002）、
# 1つでも存在しない名前が混ざると呼び出し全体が失敗する。候補を投げて、
# 弾かれたものを外しながら「実在する項目」を確かめる。
# 個人情報の入る項目（氏名・住所・電話・備考など）は候補に入れない。
ROW_CANDIDATES = [
    "receive_order_row_receive_order_id",        # 伝票番号
    "receive_order_row_shop_cut_form_id",        # 店舗の注文番号
    "receive_order_row_no",                      # 明細行番号
    "receive_order_row_shop_row_no",             # 店舗側の明細行番号
    "receive_order_row_goods_id",                # 商品コード
    "receive_order_row_goods_name",              # 商品名
    "receive_order_row_quantity",                # 受注数
    "receive_order_row_unit_price",              # 売単価
    "receive_order_row_received_time_first_cost",  # 受注時の原価
    "receive_order_row_wholesale_retail_ratio",  # 掛率
    "receive_order_row_sub_total_price",         # 小計
    "receive_order_row_goods_option",            # 商品オプション（項目選択肢）
    "receive_order_row_cancel_flag",             # 明細のキャンセル
    "receive_order_row_stock_allocation_quantity",  # 引当数
    "receive_order_row_stock_allocation_date",   # 引当日
    "receive_order_row_received_time_merchandise_id",    # 受注時の商品区分ID
    "receive_order_row_received_time_merchandise_name",  # 受注時の商品区分名
    "receive_order_row_received_time_goods_type_id",     # 受注時の取扱区分ID
    "receive_order_row_received_time_goods_type_name",   # 受注時の取扱区分名
    "receive_order_row_returned_good_quantity",  # 返品（良品）数
    "receive_order_row_returned_bad_quantity",   # 返品（不良）数
    "receive_order_row_org_row_no",              # 元の行番号（セット分解の親など）
    "receive_order_row_deleted_flag",            # 削除フラグ
    "receive_order_row_creation_date",           # 作成日
    "receive_order_row_last_modified_date",      # 最終更新日
]
BASE_CANDIDATES = [
    "receive_order_id",
    "receive_order_shop_id",                     # 店舗ID（1=楽天）
    "receive_order_shop_cut_form_id",            # 店舗の注文番号
    "receive_order_date",                        # 受注日
    "receive_order_import_date",                 # 取込日
    "receive_order_send_date",                   # 出荷確定日
    "receive_order_send_plan_date",              # 出荷予定日
    "receive_order_order_status_id",             # 受注状態
    "receive_order_cancel_type_id",              # キャンセル区分
    "receive_order_cancel_date",                 # キャンセル日
    "receive_order_gift_flag",                   # ギフト
    "receive_order_last_modified_date",          # 最終更新日
]


def _try(fields):
    """この項目の組で1件引けるか。(ok, 取れた行 or エラー文)。"""
    try:
        res = client.call(ROW_EP, {"fields": ",".join(fields), "limit": "1"})
        return True, (res.get("data") or [])
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def probe():
    """受注明細で使える項目名を確かめる。出すのは項目名だけ（値は出さない・公開ログのため）。

    まず候補を全部まとめて投げる（1回）。弾かれたら、エラー文に名前が出ている項目を
    外して投げ直す。名前が分からないときは1項目ずつ確かめる。呼び出しは CALL_BUDGET まで。
    """
    candidates = ROW_CANDIDATES + BASE_CANDIDATES
    calls = 0
    alive, dead = list(candidates), []

    # 1) まとめて投げ、エラー文に出た項目を外す
    rows = None
    for _ in range(12):
        ok, got = _try(alive)
        calls += 1
        if ok:
            rows = got
            break
        named = [f for f in alive if f in got]
        if not named:
            break
        for f in named:
            alive.remove(f)
            dead.append(f)

    # 2) それでも通らなければ、最低限の1項目に足しながら1つずつ確かめる
    if rows is None:
        base = "receive_order_row_goods_id"
        ok, got = _try([base])
        calls += 1
        if not ok:
            print("[probe] 最低限の項目（商品コード）でも取れません: {}".format(got[:160]), flush=True)
            return 1
        alive, dead = [base], []
        for f in candidates:
            if f == base:
                continue
            if calls >= CALL_BUDGET:
                print("[probe] 呼び出しの上限（{}回）に達したので打ち切ります。".format(CALL_BUDGET), flush=True)
                return 1
            ok, got = _try([base, f])
            calls += 1
            (alive if ok else dead).append(f)
        ok, rows = _try(alive)
        calls += 1
        if not ok:
            rows = []

    print("[probe] NE APIを {} 回呼びました。".format(calls), flush=True)
    filled = {}
    if rows:
        filled = {k: (v not in (None, "")) for k, v in rows[0].items()}
    print("[probe] 使える項目（{}個・値は出しません）:".format(len(alive)), flush=True)
    for f in alive:
        mark = "値あり" if filled.get(f) else ("空" if f in filled else "不明")
        print("  o {}  [{}]".format(f, mark), flush=True)
    print("[probe] 使えない項目（{}個）:".format(len(dead)), flush=True)
    for f in dead:
        print("  x {}".format(f), flush=True)
    if not rows:
        # 「項目が無い」と「受注が1件も無い」を取り違えない
        print("[probe] 項目は通りましたが、明細が1件も返りませんでした。", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description="NE受注明細の取り出し")
    parser.add_argument("--probe", action="store_true",
                        help="項目名の確認だけして終わる（API 1回）")
    args = parser.parse_args()

    if not args.probe:
        # 取り出し本体は、--probe で項目名を確かめてから実装する。
        # 推測した項目名で全件取得を組むと、1つ違うだけで全部落ちる（004002）。
        print("[ne_order_rows] いまは --probe だけ使えます。", file=sys.stderr, flush=True)
        return 2

    code = probe()
    usage.flush()
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # 例外の文面に受注の中身が混ざることがあるので、種類だけ出す
        print("[ne_order_rows] FAILED: {}".format(type(exc).__name__), file=sys.stderr, flush=True)
        print("[ne_order_rows] {}".format(str(exc)[:200]), file=sys.stderr, flush=True)
        sys.exit(1)
