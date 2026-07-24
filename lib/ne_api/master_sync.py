# -*- coding: utf-8 -*-
"""
NE商品マスタをAPIで全件取得し、Driveのマスタ（master_auto_*）として保存する。

- 週次の自動更新や、手動アップが滞ったときの「今すぐ取得」に使う。
- **最低限カラム**（商品コード/JANコード/商品名/原価/項目1/ロケーションコード）のみ取得。
  拡張が必要になったら FIELD_MAP に足す（他機能は列名で読むので順序・列数は不問）。
- 出力ヘッダは正規の日本語名にそろえるため、master_store がそのまま読める。
- 手動アップ（master_*）とAPI自動（master_auto_*）はファイル名で区別できる。

JANコードのAPIフィールド名は環境で揺れる可能性があるため、候補を1件検索して自動判定する。
"""
import pandas as pd

from . import client

# NE APIフィールド名 → マスタの正規（日本語）ヘッダ。列順は問わない（読み手が列名で解決する）。
# JAN= goods_jan_code（2026-07-24 API仕様マニュアルでユーザー確認）。
FIELD_MAP = {
    "goods_id": "商品コード",
    "goods_jan_code": "JANコード",
    "goods_name": "商品名",
    "goods_cost_price": "原価",
    "goods_1_item": "項目1",
    "goods_location": "ロケーションコード",
}
CANONICAL_ORDER = ["商品コード", "JANコード", "商品名", "原価", "項目1", "ロケーションコード"]

# APIは呼び出し回数で課金されるため、1回で多く取得して呼び出し回数を最小化する
# （10万件: 1万件/回なら約11回、1000件/回だと約101回）。
PAGE_LIMIT = 10000
MAX_PAGES = 2000      # 無限ループ防止


def available_fields(sample=1):
    """fields未指定でNEが返す商品1件のキー一覧（実在フィールドの調査用）。"""
    try:
        rows = client.call("api_v1_master_goods/search",
                           {"limit": str(sample)}).get("data") or []
        keys = set()
        for r in rows:
            keys.update(r.keys())
        return sorted(keys)
    except Exception:  # noqa: BLE001
        return []


def total_count():
    """NE商品マスタの総件数（進捗バー・終了判定用）。取れなければ0。"""
    try:
        return int(client.call("api_v1_master_goods/count", {}).get("count", 0))
    except Exception:  # noqa: BLE001
        return 0


def fetch_master(on_progress=None):
    """NE商品マスタを全件取得し、正規ヘッダのDataFrameを返す。
    返り値: (df, jan_ok)。jan_okがFalseならJAN列が空（JANスキャンに影響）。
    ※NE searchの count はそのページの件数を返すため終了判定には使わず、
      count APIの総件数(total)で offset>=total まで回す（呼び出し回数も最小化）。"""
    fields = ",".join(FIELD_MAP.keys())
    total = total_count()
    rows, offset = [], 0
    for _ in range(MAX_PAGES):
        result = client.call("api_v1_master_goods/search",
                             {"fields": fields, "limit": str(PAGE_LIMIT),
                              "offset": str(offset)})
        data = result.get("data") or []
        rows.extend(data)
        offset += len(data)
        if on_progress:
            on_progress(offset, total or offset)
        if not data or (total and offset >= total):
            break

    df = pd.DataFrame(rows)
    df = df.rename(columns={k: v for k, v in FIELD_MAP.items() if k in df.columns})
    keep = [c for c in CANONICAL_ORDER if c in df.columns]
    df = df[keep] if keep else df
    jan_ok = ("JANコード" in df.columns
              and df["JANコード"].astype(str).str.strip().replace("nan", "").ne("").any())
    return df, jan_ok


def save_master_auto(df, folder_id):
    """API取得マスタを master_auto_YYYYMMDD_NNN.csv としてDriveへ保存し、ファイル名を返す。"""
    from lib.invoice import drive_master
    data = df.to_csv(index=False, lineterminator="\r\n").encode("utf-8-sig")
    return drive_master.upload_versioned(data, "master_auto", folder_id)
