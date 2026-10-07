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


def probe():
    """受注明細を1件だけ引いて、返ってくる項目名を出す。値は出さない（公開ログのため）。

    fields を指定しないと NE は取れる項目をすべて返す（商品マスタで確認済みの挙動）。
    """
    res = client.call(ROW_EP, {"limit": "1"})
    rows = res.get("data") or []
    if not rows:
        # 「項目が無い」と「受注が1件も無い」を取り違えない
        print("[probe] 受注明細が1件も返りませんでした（APIは通っています）。", flush=True)
        return 1
    keys = sorted(rows[0].keys())
    print("[probe] 受注明細で取れる項目名（{}個・値は出しません）:".format(len(keys)), flush=True)
    for key in keys:
        value = rows[0][key]
        filled = "値あり" if value not in (None, "") else "空"
        print("  - {}  [{}]".format(key, filled), flush=True)
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
