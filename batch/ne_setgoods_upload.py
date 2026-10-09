#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ネクストエンジンのセット商品マスタへ、新しいセットを公式APIで登録する手動バッチ。

【なぜ要るか】
  モールの商品ページでセット商品を売るには、NEのセット商品マスタに「セット商品コード＝内訳」を
  登録する必要がある（NEが内訳の在庫からセットの在庫を計算してモールへ送る）。
  NEは利用規約でAI・自動化ツールによる画面の操作を禁じている（公式APIは対象外）ので、
  画面ではなくAPI（api_v1_master_setgoods/upload）で登録する。

【守ること】
  - **すでにあるセット商品コードは送らない**（送ったときに上書きか追加かが公式に書かれていない）
  - 内訳の商品コードがNEに1つでも無いセットは送らない（内訳の欠けたセットを作らない）
  - 送ったあと、NEから引き直して内訳・数量・価格・代表商品コードが計画どおりかを確かめる。
    食い違えば失敗で終わる（「送った」ことと「入った」ことは別）
  - 既定は確認だけ。--apply を付けたときだけNEへ送る

【このリポジトリは公開】
  ログに出すのは件数とファイル名だけ。セット商品コード・商品コード・価格は出さない。
  1件ごとの結果は Google ドライブ（非公開）の `ne_setgoods_result_YYYYMMDD_NNN.csv` に書く。

【計画CSV】
  Google ドライブの商品マスタと同じフォルダに置く。見出しは日本語（1行＝内訳1つ）:
    セット商品コード,セット商品名,セット商品販売価格,商品コード,数量,代表商品コード

実行:
    python batch/ne_setgoods_upload.py --file <計画CSVのファイル名>                    # 確認だけ
    python batch/ne_setgoods_upload.py --file <名前> --only <コード,コード> --apply   # 指定のセットだけ登録
    python batch/ne_setgoods_upload.py --file <名前> --apply                          # 新しいセットを全部登録
    （--no-daihyo で、代表商品コードの列を付けずに送る）

終了コード: 0=計画どおり（確認だけ・登録して照合OK・すべて登録済みで中身も一致）
            1=どれか1つでも登録できない／登録後の中身が計画と違う／通信・認証の失敗
"""
import argparse
import csv
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                              # noqa: E402
st_shim.install()                                      # libのimportより前に差し替える

from lib import master_store                           # noqa: E402
from lib.invoice import drive_master                   # noqa: E402
from lib.ne_api import client, setgoods, usage  # noqa: E402

RESULT_PREFIX = "ne_setgoods_result"
TAG = "[ne_setgoods_upload]"


def save_result(folder, rows):
    """1件ごとの結果をドライブへ保存する（版を足す。上書きしない）。ファイル名を返す。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["セット商品コード", "区分", "結果", "理由"])
    writer.writerows(rows)
    return drive_master.upload_versioned(buf.getvalue().encode("utf-8-sig"), RESULT_PREFIX, folder)


def run(file_name, only, apply, with_daihyo):
    folder = master_store.folder_id()
    found = drive_master.find_file(file_name, folder)
    if not found:
        print(f"{TAG} FAILED: 計画CSVがドライブに見つかりません（ファイル名を確かめてください）", flush=True)
        return 1
    text = drive_master.download_bytes(found["id"]).decode("utf-8-sig")
    try:
        everything = setgoods.parse_plan(text)
        sets = setgoods.pick(everything, only)
    except setgoods.PlanError as e:
        name = save_result(folder, [["", "計画CSVの誤り", "何も送っていない", str(e)]])
        print(f"{TAG} FAILED: 計画CSVに誤りがあります。NEへは何も送っていません。詳細: {name}", flush=True)
        return 1
    parts = sum(len(e["parts"]) for e in sets.values())
    print(f"{TAG} 計画: セット {len(sets)}件（計画CSV全体 {len(everything)}件）/ 内訳 {parts}行", flush=True)

    out = setgoods.execute(sets, apply, with_daihyo=with_daihyo)
    usage.flush()
    print(f"{TAG} 区分: " + " / ".join(f"{k} {v}件" for k, v in out["counts"].items()), flush=True)
    if out["sent"]:
        print(f"{TAG} 送信: セット {out['sent']}件 / 代表商品コードの列 {'あり' if out['used_daihyo'] else 'なし'}"
              f" / NEの処理 {'完了' if out['que_ok'] else '失敗'}", flush=True)
    print(f"{TAG} 照合: {out['checked']}件を引き直し / 計画と違う {out['mismatched']}件", flush=True)
    name = save_result(folder, list(out["rows"].values()))
    print(f"{TAG} 結果ファイル: {name}", flush=True)
    print(f"{TAG} {'FAILED' if out['failed'] else 'OK'}: 登録できない {out['blocked']}件 / 計画と違う {out['mismatched']}件"
          f" / {'NEへ送った' if out['sent'] else 'NEへは送っていない'}", flush=True)
    return 1 if out["failed"] else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="ドライブに置いた計画CSVのファイル名")
    parser.add_argument("--only", default="", help="登録するセット商品コード（カンマ区切り）。空なら全部")
    parser.add_argument("--apply", action="store_true", help="NEへ送る（付けなければ確認だけ）")
    parser.add_argument("--no-daihyo", action="store_true", help="代表商品コードの列を付けずに送る")
    args = parser.parse_args()
    only = [c for c in args.only.replace(" ", "").split(",") if c]
    try:
        return run(args.file.strip(), only, args.apply, not args.no_daihyo)
    except client.NEAuthError:
        print(f"{TAG} FAILED: NE APIの認証が切れています（アプリの「NE API接続」から再認可してください）", flush=True)
        return 1
    except Exception as e:  # noqa: BLE001
        # 例外の文面にコードや価格が入ることがあるので、ログには種類とNEのエラー番号だけ出す（公開リポジトリ）。
        # 全文はドライブの結果ファイルへ（鍵がここにしか無く、手元では再現できないため）。
        number = re.search(r"(?<!\d)\d{6}(?!\d)", str(e))
        try:
            name = save_result(master_store.folder_id(), [["", "実行時のエラー", "失敗", f"{type(e).__name__}: {e}"]])
        except Exception:  # noqa: BLE001
            name = "（ドライブにも書けませんでした）"
        print(f"{TAG} FAILED: {type(e).__name__}"
              f"{'（NEのエラー番号 ' + number.group(0) + '）' if number else ''}。詳細: {name}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
