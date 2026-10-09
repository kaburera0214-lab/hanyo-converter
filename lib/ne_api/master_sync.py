# -*- coding: utf-8 -*-
"""
NE商品マスタをAPIで全件取得し、Driveのマスタ（master_auto_*）として保存する。

- 週次の自動更新や、手動アップが滞ったときの「今すぐ取得」に使う。
- **最低限カラム**（商品コード/JANコード/商品名/原価/項目1/ロケーションコード）は必ず取得。
  拡張が必要になったら FIELD_MAP に足す（他機能は列名で読むので順序・列数は不問）。
- **在庫の列**（在庫数/引当数/フリー在庫数/不良在庫数/発注残数/予約在庫数）も足す（2026-10-09〜）。
  商品マスタ検索は在庫の項目も一緒に返せるので、API の呼び出し回数は増えない。
  在庫の項目が使えるかは全件取得の前に1件だけ引いて確かめ、**通った項目だけ**を足す。
  取れなかった項目は列ごと出さない（0 で埋めない。「在庫0」と「取れなかった」を取り違えないため）。
  在庫は取得した時点の値。ファイル名の日付より新しい動きは入っていない。
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

# 在庫の項目 → 列名。必須ではない（取れなくても原価のマスタとしては使える）。
# 並びは CANONICAL_ORDER の後ろ。読み手は列名で読むので、列が増えても既存の機能は変わらない。
STOCK_FIELD_MAP = {
    "stock_quantity": "在庫数",
    "stock_allocation_quantity": "引当数",
    "stock_free_quantity": "フリー在庫数",
    "stock_defective_quantity": "不良在庫数",
    "stock_remaining_order_quantity": "発注残数",
    "stock_advance_order_quantity": "予約在庫数",
}
STOCK_ORDER = list(STOCK_FIELD_MAP.values())
STOCK_KEY_COLUMN = "在庫数"     # これが無ければ「在庫を取れなかった」とみなす

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


def _try(fields):
    """この項目の組で1件引けるか。(ok, エラー文)。認証切れはそのまま上へ投げる。"""
    try:
        client.call("api_v1_master_goods/search", {"fields": ",".join(fields), "limit": "1"})
        return True, ""
    except client.NEAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_stock_fields():
    """在庫の項目のうち、この環境で使えるものを返す。(使える, 使えない)。

    まず全部まとめて1回。弾かれたら、エラー文に名前が出た項目を外して投げ直し、
    名前が分からなければ1つずつ確かめる。呼び出しは多くても「項目数+2」回。
    """
    alive, dead = list(STOCK_FIELD_MAP), []
    for _ in range(len(STOCK_FIELD_MAP)):
        if not alive:
            return alive, dead
        ok, error = _try(["goods_id"] + alive)
        if ok:
            return alive, dead
        named = [f for f in alive if f in error]
        if not named:
            break
        for f in named:
            alive.remove(f)
            dead.append(f)
    else:
        return alive, dead

    alive, dead = [], []
    for f in STOCK_FIELD_MAP:
        ok, _error = _try(["goods_id", f])
        (alive if ok else dead).append(f)
    return alive, dead


def missing_stock_columns(df):
    """このマスタに入っていない在庫の列名。空なら全部そろっている。"""
    return [c for c in STOCK_ORDER if c not in df.columns]


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
    stock_fields, _unavailable = check_stock_fields()
    field_map = dict(FIELD_MAP)
    field_map.update({f: STOCK_FIELD_MAP[f] for f in stock_fields})
    fields = ",".join(field_map.keys())
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
    df = df.rename(columns={k: v for k, v in field_map.items() if k in df.columns})
    keep = [c for c in CANONICAL_ORDER + STOCK_ORDER if c in df.columns]
    df = df[keep] if keep else df
    jan_ok = ("JANコード" in df.columns
              and df["JANコード"].astype(str).str.strip().replace("nan", "").ne("").any())
    return df, jan_ok


def save_master_auto(df, folder_id):
    """API取得マスタを master_auto_YYYYMMDD_NNN.csv としてDriveへ保存し、ファイル名を返す。"""
    from lib.invoice import drive_master
    data = df.to_csv(index=False, lineterminator="\r\n").encode("utf-8-sig")
    return drive_master.upload_versioned(data, "master_auto", folder_id)
