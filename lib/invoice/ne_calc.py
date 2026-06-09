# -*- coding: utf-8 -*-
"""
ネクストエンジン(NE)データの集計（請求書発行機能専用）。

Phase2: ①出荷確定CSVから出荷件数を集計し、受注作業料(件数×単価)を算出する。
列順は端末ごとにズレ得るため列名で参照し、必須列が無ければエラーにする。
"""
import re
import unicodedata
import pandas as pd
from . import csv_import


def _norm(s):
    """全角/半角カナの揺れを吸収（NFKC）して前後空白除去。"""
    return unicodedata.normalize("NFKC", str(s)).strip()


def _norm_columns(df):
    df.columns = [_norm(c) for c in df.columns]
    return df

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
    生データでは複数個口の場合カンマ等区切りで複数の送り状が入るため展開する。
    ⑤運賃の絞り込みに使う。列が無ければ空集合。"""
    if "発送伝票番号" not in df.columns:
        return set()
    result = set()
    for v in df["発送伝票番号"].astype(str):
        for token in re.split(r"[,、\s/]+", v):
            t = token.strip()
            if t and t.lower() != "nan":
                result.add(t)
    return result


def load_order_detail(file_bytes):
    """②受注明細一覧を読み込む。商品コード・受注数が必須。列名はNFKC正規化。"""
    df = _norm_columns(csv_import.read_csv_auto(file_bytes))
    for col in ("商品コード", "受注数"):
        if col not in df.columns:
            raise ValueError(
                f"②に必須列が見つかりません: {col} / 実際の列: {list(df.columns)}")
    return df


def load_product_master(file_bytes):
    """③NEカスタム(商品マスタ)を読み込む。商品コード・項目1が必須。"""
    df = _norm_columns(csv_import.read_csv_auto(file_bytes))
    for col in ("商品コード", "項目1"):
        if col not in df.columns:
            raise ValueError(
                f"③に必須列が見つかりません: {col} / 実際の列: {list(df.columns)}")
    return df


def compute_picking_charge(order_df, product_df, ship_rates):
    """
    出荷作業料 ＝ Σ 受注数(PCS) × 出荷作業単価(項目1サイズ)。
    商品コードで②と③を突合し、③の項目1をサイズとして単価を引く。

    ship_rates: {サイズ(=配送種別): 単価}（例 nekop=52, 60=84 ...）
    返り値: dict(出荷作業料, サイズ別PCS, 未マッチ商品数, 単価未登録サイズ)
    """
    size_map = dict(zip(product_df["商品コード"].map(_norm),
                        product_df["項目1"].map(_norm)))
    has_name = "商品名" in order_df.columns
    codes = order_df["商品コード"].map(_norm)
    pcs = pd.to_numeric(order_df["受注数"], errors="coerce").fillna(0)
    names = order_df["商品名"].astype(str) if has_name else None

    unmatched = 0
    by_size = {}
    problem_rows = []   # 単価が引けなかった明細（サイズ不明 or 単価未登録）
    total = 0.0
    for i in range(len(order_df)):
        code = codes.iloc[i]
        q = float(pcs.iloc[i])
        raw_size = size_map.get(code)
        in_master = code in size_map
        if not in_master:
            unmatched += 1
        if raw_size is None or str(raw_size) in ("", "nan"):
            key = "(不明)"
        else:
            key = str(raw_size)
        by_size[key] = by_size.get(key, 0) + q

        rate = ship_rates.get(key)
        if rate is None:
            reason = ("③商品マスタに無い" if not in_master
                      else "項目1（サイズ）が空" if key == "(不明)"
                      else f"単価マスタにサイズ'{key}'の単価が無い")
            problem_rows.append({
                "商品コード": order_df["商品コード"].iloc[i],
                "商品名": names.iloc[i] if has_name else "",
                "サイズ(項目1)": key,
                "受注数(PCS)": int(q),
                "理由": reason,
            })
        else:
            total += q * float(rate)

    missing_rate = sorted({r["サイズ(項目1)"] for r in problem_rows})
    return {
        "出荷作業料": round(total),
        "サイズ別PCS": {k: int(v) for k, v in by_size.items()},
        "未マッチ商品数": unmatched,
        "単価未登録サイズ": missing_rate,
        "未登録明細": problem_rows,
    }


def build_picking_detail(order_df, product_df, ship_rates, shime_date=""):
    """
    出荷作業費のフル明細を返す（1行＝1商品）。
    列: 日付, 商品コード, 商品名, サイズ, 数量, 単価, 計
    """
    size_map = dict(zip(product_df["商品コード"].map(_norm),
                        product_df["項目1"].map(_norm)))
    has_name = "商品名" in order_df.columns
    rows = []
    for i in range(len(order_df)):
        code = _norm(order_df["商品コード"].iloc[i])
        size = size_map.get(code, "")
        if size in ("", "nan"):
            size = "(不明)"
        qty = float(pd.to_numeric(order_df["受注数"], errors="coerce").fillna(0).iloc[i])
        price = float(ship_rates.get(size, 0))
        rows.append({
            "日付": shime_date,
            "商品コード": str(order_df["商品コード"].iloc[i]),
            "商品名": str(order_df["商品名"].iloc[i]) if has_name else "",
            "サイズ": size,
            "数量": int(qty),
            "単価": price,
            "計": round(qty * price),
        })
    return pd.DataFrame(
        rows, columns=["日付", "商品コード", "商品名", "サイズ", "数量", "単価", "計"])


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
