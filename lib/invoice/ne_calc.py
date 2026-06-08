# -*- coding: utf-8 -*-
"""
ネクストエンジン(NE)データの集計（請求書発行機能専用）。

Phase2: ①出荷確定CSVから出荷件数を集計し、受注作業料(件数×単価)を算出する。
列順は端末ごとにズレ得るため列名で参照し、必須列が無ければエラーにする。
"""
import pandas as pd
from . import csv_import

# ①出荷確定CSVの必須列（これが無ければ取込エラー）
REQUIRED_SHIPMENT_COLS = ["伝票番号", "発送方法"]


def load_shipment(file_bytes):
    """
    ①NE出荷確定CSVを読み込み、列名の前後空白を除去したDataFrameを返す。
    必須列が欠けていればValueError。
    """
    df = csv_import.read_csv_auto(file_bytes)
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_SHIPMENT_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"必須列が見つかりません: {', '.join(missing)} / "
            f"実際の列: {list(df.columns)}")
    return df


def summarize_shipment(df):
    """
    出荷件数（ユニーク伝票番号数）と、発送方法別の件数を返す。
    返り値: {"出荷件数": int, "発送方法別": {発送方法: 件数, ...}}
    """
    denpyo = df["伝票番号"].astype(str).str.strip()
    denpyo = denpyo[(denpyo != "") & (denpyo.str.lower() != "nan")]
    count = int(denpyo.nunique())

    method = df["発送方法"].astype(str).str.strip()
    by_method = method[method != ""].value_counts().to_dict()
    return {"出荷件数": count, "発送方法別": by_method}


def get_soufuda_set(df):
    """①出荷確定の「発送伝票番号」（ヤマト送り状番号）の集合を返す。
    ⑤運賃の絞り込みに使う。列が無ければ空集合。"""
    if "発送伝票番号" not in df.columns:
        return set()
    s = df["発送伝票番号"].astype(str).str.strip()
    return set(s[(s != "") & (s.str.lower() != "nan")])


def get_ne_denpyo_set(df):
    """①出荷確定の「伝票番号」（NE伝票番号）の集合を返す。
    ④発行済データ(品名２)との橋渡しに使う。"""
    if "伝票番号" not in df.columns:
        return set()
    s = df["伝票番号"].astype(str).str.strip()
    return set(s[(s != "") & (s.str.lower() != "nan")])


def classify_delivery_rough(method_name):
    """
    発送方法の文字列から配送区分をざっくり判定（Phase3で⑤と突合する際の補助）。
    ネコポス系→'nekop'、それ以外のヤマト宅配便→'宅配便(サイズ不明)'。
    """
    s = str(method_name)
    if "ネコポス" in s or "nekop" in s.lower() or "メール便" in s:
        return "nekop"
    return "宅配便(サイズ不明)"
