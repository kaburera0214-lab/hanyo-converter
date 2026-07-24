# -*- coding: utf-8 -*-
"""
入荷登録の「資材ナンバー・ロケーションマスタ」（Drive保存・画面で編集）。

プルダウンの選択肢は、当初はNE商品マスタのロケーションコード既存値から自動生成して
いたが、誤登録が混ざるため、この画面で管理する専用マスタに切り替えた（2026-07-22確定）。
正本は Drive の receiving_master.json。初期値は同梱の data/locations.csv（ロケ一覧.xlsx由来）。

ロケーションは3階層（第一階層=エリア／第二階層=棚・列／第三階層=段）で、
作業者は上位から順に選ぶ。**NEに書くロケーションコードは最下層の値**:
  トイプー / TA / TA10B → TA10B      （3階層あるもの）
  梱包室 / CB1 / (空)   → CB1        （2階層までのもの）
配送サイズは従来どおり送料・資材マスタ（pricing_cost_master.csv）のキーを使う。
"""
import json
import os

import pandas as pd

from lib.invoice import drive_master
from lib.pricing import masters as pmasters

MASTER_NAME = "receiving_master.json"
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

LOC_COLUMNS = ["第一階層", "第二階層", "第三階層"]

# 初期の資材ナンバー（2026-07-22ユーザー確定の19種）
DEFAULT_MATERIALS = ["60A", "60B", "60C", "80A", "80B", "100A", "100B",
                     "120A", "120B", "140A", "140B", "160A", "160B",
                     "MB2", "MB3", "MB4", "MB5", "ND", "ST"]


def _norm(value):
    """NFKC正規化・空白除去。NaN/None/空は空文字に。"""
    s = pmasters.norm_key(value)
    return "" if s.lower() in ("nan", "none") else s


def _norm_list(values):
    """正規化・空値/重複除去。登録順は保持する。"""
    out = []
    for v in values:
        s = _norm(v)
        if s and s not in out:
            out.append(s)
    return out


def norm_locations(rows):
    """ロケーション行を正規化する。
    rows: [(第一階層, 第二階層, 第三階層)] または [{"l1":..,"l2":..,"l3":..}] または旧形式の文字列
    返り値: [(l1, l2, l3)]（第一・第二階層が空の行は捨て、最下層コードの重複も除去）"""
    out, seen = [], set()
    for r in rows:
        if isinstance(r, str):          # 旧形式（フラットな文字列リスト）からの移行
            l1, l2, l3 = "", _norm(r), ""
        elif isinstance(r, dict):
            l1, l2, l3 = _norm(r.get("l1")), _norm(r.get("l2")), _norm(r.get("l3"))
        else:
            vals = list(r) + ["", "", ""]
            l1, l2, l3 = _norm(vals[0]), _norm(vals[1]), _norm(vals[2])
        if not l2:                      # 第二階層まではロケーションの必須要素
            continue
        code = l3 or l2
        if code in seen:
            continue
        seen.add(code)
        out.append((l1, l2, l3))
    return out


def location_code(row):
    """ロケーション行 → NEに書くロケーションコード（最下層の値）。"""
    l1, l2, l3 = row
    return l3 or l2


def load_bundled_locations():
    """同梱の初期ロケーション一覧（ロケ一覧.xlsx由来）を読む。"""
    path = os.path.join(_DATA_DIR, "locations.csv")
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    return norm_locations(df[LOC_COLUMNS].itertuples(index=False, name=None))


def locations_to_df(rows):
    """ロケーション行 → 画面編集用DataFrame。"""
    return pd.DataFrame(list(rows), columns=LOC_COLUMNS)


def hierarchy(rows):
    """ロケーション行 → 階層選択用の入れ子dict {第一階層: {第二階層: [第三階層…]}}。
    第三階層が無い（2階層まで）の場合は空リストになる。"""
    tree = {}
    for l1, l2, l3 in rows:
        lv2 = tree.setdefault(l1, {})
        lv3 = lv2.setdefault(l2, [])
        if l3:
            lv3.append(l3)
    return tree


def flat_options(rows):
    """ロケーション行 → [(表示ラベル, ロケーションコード)]。
    まとめて入力の1列プルダウン用（階層をラベルに含めて探しやすくする）。"""
    out = []
    for row in rows:
        l1, l2, l3 = row
        label = " ｜ ".join([p for p in (l1, l2, l3) if p])
        out.append((label, location_code(row)))
    return out


def load(folder_id):
    """入荷登録マスタ {materials:[...], locations:[(l1,l2,l3)…]} を読む。無ければ空の構造。"""
    try:
        f = drive_master.find_file(MASTER_NAME, folder_id)
        if f:
            data = json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
            return {"materials": _norm_list(data.get("materials", [])),
                    "locations": norm_locations(data.get("locations", []))}
    except Exception:  # noqa: BLE001
        pass
    return {"materials": [], "locations": []}


def save(materials, locations, folder_id):
    """入荷登録マスタをDriveへ保存（上書き）。正規化後の内容を返す。"""
    mats = _norm_list(materials)
    locs = norm_locations(locations)
    payload = json.dumps({"materials": mats,
                          "locations": [list(r) for r in locs]},
                         ensure_ascii=False, indent=2).encode("utf-8")
    drive_master.upload_or_replace(payload, MASTER_NAME, folder_id,
                                   mimetype="application/json")
    return {"materials": mats, "locations": locs}
