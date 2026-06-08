# -*- coding: utf-8 -*-
"""
ヤマト運賃データ(⑤運賃情報参照)の集計（請求書発行機能専用 / Phase3）。

①NE出荷確定の「発送伝票番号」で全件のヤマト運賃を絞り込み、
配送種別(サイズ)別に 出荷作業料・資材費 を、運賃から 送料 を算出する。

紐付け検証済み: ①発送伝票番号 ⇔ ⑤原票No.（ハイフン除去）で直接一致。
⑤サイズ列: "－"＝ネコポス→nekop、それ以外は 60/80/100/120/140/160。
"""
import unicodedata
import pandas as pd
from . import csv_import

# ⑤運賃CSVの必須列
REQUIRED_FREIGHT_COLS = ["原票No.", "サイズ"]
# 実費送料に使う運賃列（税別。MF側で10%加算されるため税別を使用）
FREIGHT_AMOUNT_COL = "運賃等合計(税別)"
PREF_COL = "扱店都道府県"


def _digits(value):
    """ハイフン等を除いた数字文字列にする（送り状番号の正規化）。"""
    return "".join(ch for ch in str(value) if ch.isdigit())


def size_to_delivery_type(size_value):
    """⑤サイズ列の値を配送種別へ。全角数字も半角化。
    "－"/空→nekop、"６０"/"60サイズ"/"60"→"60"。"""
    s = unicodedata.normalize("NFKC", str(size_value)).strip()
    if s in ("", "-", "－", "nan"):
        return "nekop"
    # ネコポス/メール便系（"3cm以内"・"ネコポス"・"メール便"）はnekop
    low = s.lower()
    if "cm" in low or "ネコポス" in s or "メール" in s or "nekop" in low:
        return "nekop"
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits if digits else "nekop"


def load_freight(file_bytes):
    """⑤運賃CSVを読み込む。必須列が無ければValueError。"""
    df = csv_import.read_csv_auto(file_bytes)
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_FREIGHT_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"必須列が見つかりません: {', '.join(missing)} / 実際の列: {list(df.columns)}")
    return df


def filter_by_soufuda(freight_df, soufuda_set):
    """
    送り状番号集合（①発送伝票番号）でヤマト運賃を絞り込む。
    原票No.はハイフン除去して数字一致で判定。
    返り値: (絞り込み後DataFrame, マッチ件数, 全件数)
    """
    keys = {_digits(x) for x in soufuda_set if _digits(x)}
    genpyo = freight_df["原票No."].map(_digits)
    mask = genpyo.isin(keys)
    return freight_df[mask].copy(), int(mask.sum()), len(freight_df)


def _to_number(value):
    s = str(value).strip().replace(",", "")
    if s in ("", "-", "－", "nan"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def compute_charges(matched_df, *, ship_rates, material_rates,
                    shipping_method, shipping_table=None, area_map=None,
                    margin_rate=0.0, addon=0.0):
    """
    絞り込み済みのヤマト運賃から各費目を算出する。

    ship_rates / material_rates: {配送種別: 単価}
    shipping_method: "送料表" or "実費マージン"
    shipping_table: [{配送業者,配送区分,サイズ, 地域:運賃...}]（送料表方式時）
    area_map: {都道府県: エリア}（送料表方式時）
    margin_rate(%) / addon: 実費方式時の上乗せ

    返り値: dict(送料, 出荷作業料, 資材費, 種別別件数, 警告list)
    """
    warnings = []
    types = matched_df["サイズ"].map(size_to_delivery_type)
    count_by_type = types.value_counts().to_dict()

    # 出荷作業料・資材費（配送種別ごと件数×単価）
    ship_total = 0.0
    mat_total = 0.0
    for t, cnt in count_by_type.items():
        if t in ship_rates:
            ship_total += cnt * float(ship_rates[t])
        else:
            warnings.append(f"出荷作業料: 配送種別 '{t}'({cnt}件) の単価が未登録")
        if t in material_rates:
            mat_total += cnt * float(material_rates[t])
        else:
            warnings.append(f"資材費: 配送種別 '{t}'({cnt}件) の単価が未登録")

    # 送料
    ship_fee = 0.0
    if shipping_method == "送料表":
        if not shipping_table or not area_map:
            warnings.append("送料表または地域マスタが未設定のため送料を0としました")
        elif PREF_COL not in matched_df.columns:
            warnings.append(f"運賃CSVに '{PREF_COL}' 列が無いため送料表を引けません")
        else:
            # サイズ→送料表行（配送種別をキーに）。nekopはメール便行。
            table_by_type = {}
            for row in shipping_table:
                key = size_to_delivery_type(row.get("サイズ", ""))
                table_by_type[key] = row
            miss = set()
            for _, r in matched_df.iterrows():
                t = size_to_delivery_type(r["サイズ"])
                pref = str(r.get(PREF_COL, "")).strip()
                area = area_map.get(pref)
                trow = table_by_type.get(t)
                if trow is None or area is None or area not in trow:
                    miss.add((t, pref))
                    continue
                ship_fee += float(trow.get(area) or 0)
            if miss:
                warnings.append(f"送料表で引けなかった組合せ {len(miss)}種（種別/都道府県）")
    else:  # 実費マージン
        if FREIGHT_AMOUNT_COL not in matched_df.columns:
            warnings.append(f"運賃CSVに '{FREIGHT_AMOUNT_COL}' 列が無いため送料を0としました")
        else:
            base = matched_df[FREIGHT_AMOUNT_COL].map(_to_number).sum()
            ship_fee = base * (1 + float(margin_rate) / 100.0) + float(addon) * len(matched_df)

    return {
        "送料": round(ship_fee),
        "出荷作業料": round(ship_total),
        "資材費": round(mat_total),
        "種別別件数": count_by_type,
        "警告": warnings,
    }
