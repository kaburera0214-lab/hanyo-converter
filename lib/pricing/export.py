# -*- coding: utf-8 -*-
"""
価格改定の出力CSV生成（楽天RMS・Yahoo!ショッピング・ネクストエンジン）。

列構成は実際のアップロード実績ファイル（2024-01-16 keiの価格変更）に合わせている:
  楽天  normal-item.csv: 商品管理番号（商品URL）,商品番号,SKU管理番号,システム連携用SKU番号,販売価格,表示価格
        …親行（商品番号のみ）＋SKU行。SKU管理番号は楽天側の採番のためSKU対応表（masters）で引く。
  Yahoo data.csv: code,price …親コード単位・税込
  NE    商品マスタ更新CSV: syohin_code,baika_tnk,genka_tnk …税抜売価・原価

形式が変わったら、このファイルの列定義だけを直せばよいように分離してある。
"""
import io

import pandas as pd

from . import masters

# 文字コード: 3システムともShift-JIS系での取込
UPLOAD_ENCODING = "cp932"

RAKUTEN_COLUMNS = ["商品管理番号（商品URL）", "商品番号", "SKU管理番号",
                   "システム連携用SKU番号", "販売価格", "表示価格"]
YAHOO_COLUMNS = ["code", "price"]
NE_COLUMNS = ["syohin_code", "baika_tnk", "genka_tnk"]


def _to_csv_bytes(df, encoding=UPLOAD_ENCODING):
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\r\n")
    return buf.getvalue().encode(encoding, errors="replace")


def rakuten_rows(rows, sku_table):
    """
    rows: [{商品コード, 楽天販売価格}] → (normal-item.csv用の行list, SKU対応表に無いコードlist)

    SKU対応表にあればその親/SKU番号を使う。無い場合、枝番なしコードは
    「単品SKU（SKU管理番号=商品コード・連携番号なし）」とみなして出力し、
    枝番付き（-01等）はSKU管理番号が分からないため除外して missing で返す。
    """
    groups = {}   # 親コード → [(SKU管理番号, 連携番号, 価格)]
    order = []    # 親コードの出現順
    missing = []
    for r in rows:
        code = masters.norm_key(r["商品コード"])
        price = int(r["楽天販売価格"])
        hit = sku_table.get(code.lower())
        if hit:
            parent, sku_no, renkei = hit
        elif code == masters.parent_code(code):
            parent, sku_no, renkei = code, code, ""
        else:
            missing.append(code)
            continue
        key = parent.lower()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((sku_no, renkei, price))

    records = []
    for parent in order:
        records.append({"商品管理番号（商品URL）": parent, "商品番号": parent,
                        "SKU管理番号": "", "システム連携用SKU番号": "",
                        "販売価格": "", "表示価格": ""})
        for sku_no, renkei, price in groups[parent]:
            records.append({"商品管理番号（商品URL）": parent, "商品番号": "",
                            "SKU管理番号": sku_no, "システム連携用SKU番号": renkei,
                            "販売価格": price, "表示価格": price})
    return records, missing


def rakuten_csv(records):
    """rakuten_rows()の結果 → normal-item.csv のbytes。"""
    return _to_csv_bytes(pd.DataFrame(records, columns=RAKUTEN_COLUMNS))


def yahoo_rows(rows, sku_table):
    """
    rows: [{商品コード, Yahoo販売価格}] → (data.csv用の行list, SKU内で価格が割れた親コードlist)

    Yahooは親コード単位・1価格のため、同じ親に複数SKUがある場合は最高値を採用する。
    価格が割れた親は diff で返す（画面で注意喚起する用）。
    """
    prices = {}   # 親コード → [価格]
    order = []
    for r in rows:
        code = masters.norm_key(r["商品コード"])
        hit = sku_table.get(code.lower())
        parent = (hit[0] if hit else masters.parent_code(code)).lower()
        if parent not in prices:
            prices[parent] = []
            order.append(parent)
        prices[parent].append(int(r["Yahoo販売価格"]))
    records = [{"code": p, "price": max(prices[p])} for p in order]
    diff = [p for p in order if len(set(prices[p])) > 1]
    return records, diff


def yahoo_csv(records):
    """yahoo_rows()の結果 → data.csv のbytes。"""
    return _to_csv_bytes(pd.DataFrame(records, columns=YAHOO_COLUMNS))


def ne_csv(rows):
    """rows: [{商品コード, NE売価, NE原価}] → NE商品マスタ更新CSVのbytes。
    baika_tnk=税抜売価（シートの「NE：再設定売価」）、genka_tnk=原価（新下代）。"""
    df = pd.DataFrame([{
        "syohin_code": str(r["商品コード"]),
        "baika_tnk": int(r["NE売価"]),
        "genka_tnk": r["NE原価"],
    } for r in rows], columns=NE_COLUMNS)
    return _to_csv_bytes(df)


def ne_item1_csv(rows):
    """梱包サイズ変更用: [{商品コード, 新項目1}] → NE項目1更新CSVのbytes。"""
    df = pd.DataFrame([{
        "syohin_code": str(r["商品コード"]),
        "項目1": str(r["新項目1"]),
    } for r in rows], columns=["syohin_code", "項目1"])
    return _to_csv_bytes(df)


def detail_csv(df):
    """計算明細（画面の表そのまま）をExcelで開けるUTF-8(BOM)で出力。"""
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\r\n")
    return buf.getvalue().encode("utf-8-sig")
