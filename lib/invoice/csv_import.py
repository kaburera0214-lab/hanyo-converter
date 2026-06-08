# -*- coding: utf-8 -*-
"""
CSV取込ユーティリティ（請求書発行機能専用）。

送料表など、スプレッドシートで管理している複雑なマスタを
CSVで丸ごと取り込むためのパーサ群。文字コードの自動判定、
桁区切りカンマ（"1,460"）の除去などを行う。
"""
import io
import re
import pandas as pd


def read_csv_auto(file_bytes):
    """
    bytesからDataFrameを読む。UTF-8(BOM可)→CP932の順で文字コードを試す。
    全セルは文字列として読み込む（数値変換は呼び出し側で行う）。
    """
    last_err = None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return pd.read_csv(io.BytesIO(file_bytes), dtype=str, encoding=enc).fillna("")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ValueError(f"CSVを読み込めませんでした（文字コード不明）: {last_err}")


def _to_number(value):
    """ "1,460" や " 930 " を 1460/930 に変換。空欄は0。"""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    if s == "" or s == "-" or s == "－":
        return 0
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return 0


def _normalize_size(value):
    """ "60サイズ" -> "60"、"3cm以内" はそのまま。前後空白除去。"""
    s = str(value).strip()
    m = re.fullmatch(r"(\d+)\s*サイズ", s)
    return m.group(1) if m else s


def parse_shipping_table_csv(file_bytes, areas):
    """
    送料表CSVを取り込み、[{配送業者,配送区分,サイズ, 各エリア:運賃}] を返す。
    想定ヘッダ: 配送業者,配送区分,サイズ,<地域...>
    地域列は areas（送料表マスタの地域順）に存在するものだけ採用する。
    """
    df = read_csv_auto(file_bytes)
    cols = {c.strip(): c for c in df.columns}
    required = ["配送業者", "配送区分", "サイズ"]
    missing = [c for c in required if c not in cols]
    if missing:
        raise ValueError(
            f"必須列が見つかりません: {', '.join(missing)} / 実際の列: {list(df.columns)}")

    rows = []
    for _, r in df.iterrows():
        size = _normalize_size(r[cols["サイズ"]])
        if not size:
            continue
        rec = {
            "配送業者": str(r[cols["配送業者"]]).strip(),
            "配送区分": str(r[cols["配送区分"]]).strip(),
            "サイズ": size,
        }
        for area in areas:
            rec[area] = _to_number(r[cols[area]]) if area in cols else 0
        rows.append(rec)
    return rows
