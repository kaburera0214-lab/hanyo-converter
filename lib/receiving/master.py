# -*- coding: utf-8 -*-
"""
入荷登録の「資材ナンバー・ロケーションマスタ」（Drive保存・画面で編集）。

プルダウンの選択肢（資材ナンバー・ロケーション）は、当初はNE商品マスタの
ロケーションコード既存値から自動生成していたが、誤登録が混ざるため、
この画面で管理する専用マスタに切り替えた（2026-07-22ユーザー確定）。
正本は Drive の receiving_master.json（1ファイル・JSON）。配送サイズは
従来どおり送料・資材マスタ（pricing_cost_master.csv）のキーを使う。
"""
import json

from lib.invoice import drive_master
from lib.pricing import masters as pmasters

MASTER_NAME = "receiving_master.json"

# 初期の資材ナンバー（2026-07-22ユーザー確定の19種）。ロケーションは初回にNEの実値から採取する。
DEFAULT_MATERIALS = ["60A", "60B", "60C", "80A", "80B", "100A", "100B",
                     "120A", "120B", "140A", "140B", "160A", "160B",
                     "MB2", "MB3", "MB4", "MB5", "ND", "ST"]


def _norm_list(values):
    """正規化（NFKC・末尾.0除去）・空値/重複除去。登録順は保持する。"""
    out = []
    for v in values:
        s = pmasters.norm_key(v)
        if s and s.lower() not in ("nan", "none") and s not in out:
            out.append(s)
    return out


def load(folder_id):
    """入荷登録マスタ {materials:[...], locations:[...]} を読む。無ければ空の構造。"""
    try:
        f = drive_master.find_file(MASTER_NAME, folder_id)
        if f:
            data = json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
            return {"materials": _norm_list(data.get("materials", [])),
                    "locations": _norm_list(data.get("locations", []))}
    except Exception:  # noqa: BLE001
        pass
    return {"materials": [], "locations": []}


def save(materials, locations, folder_id):
    """入荷登録マスタをDriveへ保存（上書き）。正規化後の内容を返す。"""
    data = {"materials": _norm_list(materials), "locations": _norm_list(locations)}
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    drive_master.upload_or_replace(payload, MASTER_NAME, folder_id,
                                   mimetype="application/json")
    return data
