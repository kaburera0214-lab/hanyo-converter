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

# 二重価格文言管理番号: 表示価格を設定する場合に必須。1=当店通常価格（固定）
RAKUTEN_COLUMNS = ["商品管理番号（商品URL）", "商品番号", "SKU管理番号",
                   "システム連携用SKU番号", "販売価格", "表示価格",
                   "二重価格文言管理番号"]
YAHOO_COLUMNS = ["code", "price"]
NE_COLUMNS = ["syohin_code", "baika_tnk", "genka_tnk"]

# 直送タブ用の送料無料フラグ（販売価格が送料込みのため）。列名・値はモール仕様に合わせて調整
RAKUTEN_FREE_SHIP_COLUMN = "送料"          # 楽天: 0=送料別 / 1=送料無料（商品行に設定）
RAKUTEN_FREE_SHIP_VALUE = 1                # 2026-07-17 楽天仕様書でユーザー確認済み
YAHOO_FREE_SHIP_COLUMN = "postage-set"     # Yahoo: 送料設定番号（送料無料の設定番号に要調整）
YAHOO_FREE_SHIP_VALUE = 1


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
                        "販売価格": "", "表示価格": "", "二重価格文言管理番号": ""})
        for sku_no, renkei, price in groups[parent]:
            records.append({"商品管理番号（商品URL）": parent, "商品番号": "",
                            "SKU管理番号": sku_no, "システム連携用SKU番号": renkei,
                            "販売価格": price, "表示価格": price,
                            "二重価格文言管理番号": 1})
    return records, missing


def rakuten_csv(records, free_shipping=False):
    """rakuten_rows()の結果 → normal-item.csv のbytes。
    free_shipping=True（直送品・送料込み価格）なら送料無料フラグ列を追加（商品行に0）。"""
    columns = list(RAKUTEN_COLUMNS)
    if free_shipping:
        columns.append(RAKUTEN_FREE_SHIP_COLUMN)
        records = [{**r, RAKUTEN_FREE_SHIP_COLUMN:
                    (RAKUTEN_FREE_SHIP_VALUE if r.get("商品番号") else "")}
                   for r in records]
    return _to_csv_bytes(pd.DataFrame(records, columns=columns))


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


def yahoo_csv(records, free_shipping=False):
    """yahoo_rows()の結果 → data.csv のbytes。
    free_shipping=True（直送品・送料込み価格）なら送料無料フラグ列を追加。"""
    columns = list(YAHOO_COLUMNS)
    if free_shipping:
        columns.append(YAHOO_FREE_SHIP_COLUMN)
        records = [{**r, YAHOO_FREE_SHIP_COLUMN: YAHOO_FREE_SHIP_VALUE} for r in records]
    return _to_csv_bytes(pd.DataFrame(records, columns=columns))


def ne_csv(rows):
    """rows: [{商品コード, NE売価, NE原価}] → NE商品マスタ更新CSVのbytes。
    baika_tnk=税抜売価（シートの「NE：再設定売価」）、genka_tnk=原価（新下代）。"""
    df = pd.DataFrame([{
        "syohin_code": str(r["商品コード"]),
        "baika_tnk": int(r["NE売価"]),
        "genka_tnk": r["NE原価"],
    } for r in rows], columns=NE_COLUMNS)
    return _to_csv_bytes(df)


# 梱包サイズ変更: 便種が変わったときのモール配送設定の修正（2026-07-17ユーザー確定）
RAKUTEN_DELIVERY_SET_NAME = {"宅配便": "宅配便のみ", "メール便": "メール便"}  # 楽天の配送方法セット名
YAHOO_DELIVERY_COLUMN = "配送グループ管理番号"   # Yahoo側の項目名（実物に合わせて調整可）
YAHOO_DELIVERY_VALUE = {"宅配便": "NT", "メール便": "NM"}


def rakuten_delivery_csv(rows):
    """[{商品管理番号, 商品コード, 旧便種, 新便種}] → 楽天の配送方法セット修正リスト。
    現状はRMS画面で手直しするための作業リスト（API自動化はフィールド特定後に対応予定）。"""
    df = pd.DataFrame([{
        "商品管理番号": r["商品管理番号"],
        "新しい配送方法セット": RAKUTEN_DELIVERY_SET_NAME.get(r["新便種"], r["新便種"]),
        "変更内容": f"{r['旧便種']}→{r['新便種']}",
        "対象商品コード": r["商品コード"],
    } for r in rows], columns=["商品管理番号", "新しい配送方法セット", "変更内容", "対象商品コード"])
    return _to_csv_bytes(df)


def yahoo_delivery_csv(rows):
    """[{商品管理番号, 新便種}] → Yahooの配送グループ管理番号 更新CSV（宅配便=NT／メール便=NM）。"""
    df = pd.DataFrame([{
        "code": str(r["商品管理番号"]).lower(),
        YAHOO_DELIVERY_COLUMN: YAHOO_DELIVERY_VALUE.get(r["新便種"], r["新便種"]),
    } for r in rows], columns=["code", YAHOO_DELIVERY_COLUMN])
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
