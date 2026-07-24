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
FIELD_MAP = {
    "goods_id": "商品コード",
    "goods_name": "商品名",
    "goods_cost_price": "原価",
    "goods_1_item": "項目1",
    "goods_location": "ロケーションコード",
}
# JANのフィールド名候補（先に1件検索して実在するものを採用する）
JAN_CANDIDATES = ["goods_jan_cd", "goods_jan", "goods_jancd", "jan_cd", "jan_code"]
CANONICAL_ORDER = ["商品コード", "JANコード", "商品名", "原価", "項目1", "ロケーションコード"]

PAGE_LIMIT = 1000     # NE searchの1回あたり取得件数（安全側）
MAX_PAGES = 2000      # 無限ループ防止


def detect_jan_field():
    """JANコードのAPIフィールド名を1件検索で自動判定する。見つからなければNone。"""
    for cand in JAN_CANDIDATES:
        try:
            rows = client.call("api_v1_master_goods/search",
                               {"fields": f"goods_id,{cand}", "limit": "1"}).get("data") or []
        except Exception:  # noqa: BLE001
            continue      # 無効なフィールド名はNEがエラーにする → 次の候補へ
        if rows and cand in rows[0]:
            return cand
    return None


def fetch_master(on_progress=None):
    """NE商品マスタを全件取得し、正規ヘッダのDataFrameを返す。
    返り値: (df, jan_field or None)。jan_fieldがNoneならJANが取得できていない。"""
    jan = detect_jan_field()
    field_map = dict(FIELD_MAP)
    if jan:
        field_map[jan] = "JANコード"
    fields = ",".join(field_map.keys())

    rows, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        result = client.call("api_v1_master_goods/search",
                             {"fields": fields, "limit": str(PAGE_LIMIT),
                              "offset": str(offset)})
        data = result.get("data") or []
        if total is None:
            try:
                total = int(result.get("count", 0))
            except (TypeError, ValueError):
                total = 0
        rows.extend(data)
        offset += len(data)
        if on_progress:
            on_progress(offset, total or offset)
        if not data or (total and offset >= total):
            break

    df = pd.DataFrame(rows)
    # 正規ヘッダにリネーム（存在する列だけ）。列順・列数は問わない。
    df = df.rename(columns={k: v for k, v in field_map.items() if k in df.columns})
    keep = [c for c in CANONICAL_ORDER if c in df.columns]
    return df[keep] if keep else df, jan


def save_master_auto(df, folder_id):
    """API取得マスタを master_auto_YYYYMMDD_NNN.csv としてDriveへ保存し、ファイル名を返す。"""
    from lib.invoice import drive_master
    data = df.to_csv(index=False, lineterminator="\r\n").encode("utf-8-sig")
    return drive_master.upload_versioned(data, "master_auto", folder_id)
