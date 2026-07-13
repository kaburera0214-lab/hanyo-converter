# -*- coding: utf-8 -*-
"""
価格改定の出力CSV生成（楽天RMS・Yahoo!ショッピング・ネクストエンジン）。

まずは各システムの標準的な一括更新フォーマットで出力する。
実際にアップロードして列が合わない場合は、このファイルの列定義
（*_COLUMNS / ヘッダ名）だけを直せばよいように分離してある。
"""
import io

import pandas as pd

# 文字コード: 3システムともShift-JIS系での取込が標準
UPLOAD_ENCODING = "cp932"

# 楽天RMS item.csv（コントロールカラム u = 更新）
RAKUTEN_COLUMNS = ["コントロールカラム", "商品管理番号（商品URL）", "販売価格"]
RAKUTEN_CONTROL = "u"

# Yahoo!ショッピング data.csv（code=商品コード, price=通常販売価格）
YAHOO_COLUMNS = ["code", "price"]

# ネクストエンジン 商品マスタ一括更新CSV
NE_COLUMNS = ["商品コード", "売価", "原価"]


def _to_csv_bytes(df, encoding=UPLOAD_ENCODING):
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\r\n")
    return buf.getvalue().encode(encoding, errors="replace")


def rakuten_csv(rows):
    """rows: [{商品コード, 楽天販売価格}] → 楽天RMS item.csv（更新）のbytes。
    商品管理番号は楽天の仕様どおり小文字にする。"""
    df = pd.DataFrame([{
        "コントロールカラム": RAKUTEN_CONTROL,
        "商品管理番号（商品URL）": str(r["商品コード"]).lower(),
        "販売価格": int(r["楽天販売価格"]),
    } for r in rows], columns=RAKUTEN_COLUMNS)
    return _to_csv_bytes(df)


def yahoo_csv(rows):
    """rows: [{商品コード, Yahoo販売価格}] → Yahoo data.csv のbytes。"""
    df = pd.DataFrame([{
        "code": str(r["商品コード"]).lower(),
        "price": int(r["Yahoo販売価格"]),
    } for r in rows], columns=YAHOO_COLUMNS)
    return _to_csv_bytes(df)


def ne_csv(rows):
    """rows: [{商品コード, NE売価, NE原価}] → NE商品マスタ一括更新CSVのbytes。
    売価は税抜（シートの「NE：再設定売価」）、原価=新下代。"""
    df = pd.DataFrame([{
        "商品コード": str(r["商品コード"]),
        "売価": int(r["NE売価"]),
        "原価": r["NE原価"],
    } for r in rows], columns=NE_COLUMNS)
    return _to_csv_bytes(df)


def ne_item1_csv(rows):
    """梱包サイズ変更用: [{商品コード, 新項目1}] → NE項目1更新CSVのbytes。"""
    df = pd.DataFrame([{
        "商品コード": str(r["商品コード"]),
        "項目1": str(r["新項目1"]),
    } for r in rows], columns=["商品コード", "項目1"])
    return _to_csv_bytes(df)


def detail_csv(df):
    """計算明細（画面の表そのまま）をExcelで開けるUTF-8(BOM)で出力。"""
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\r\n")
    return buf.getvalue().encode("utf-8-sig")
