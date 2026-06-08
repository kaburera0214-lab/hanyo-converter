# -*- coding: utf-8 -*-
"""
MFクラウド請求書インポート用CSVの生成。

実サンプル（【経理】TeamEC請求書）の列構成・行構成に完全準拠する。
1ファイル内に「請求書」行（ヘッダ）1行＋「品目」行N行を縦に並べる方式。
このモジュールは請求書発行ページ専用で、他機能には一切依存しない。
"""
import csv
import io
from decimal import Decimal, ROUND_HALF_UP

# MFクラウドCSVの列順（サンプルの1行目をそのまま採用。変更不可）
MF_COLUMNS = [
    "csv_type(変更不可)", "行形式", "取引先名称", "件名", "請求日", "お支払期限",
    "請求書番号", "売上計上日", "メモ", "タグ", "小計", "消費税", "合計金額",
    "取引先敬称", "取引先郵便番号", "取引先都道府県", "取引先住所1", "取引先住所2",
    "取引先部署", "取引先担当者役職", "取引先担当者氏名", "自社担当者氏名", "備考",
    "振込先", "入金ステータス", "メール送信ステータス", "郵送ステータス",
    "ダウンロードステータス", "納品日", "品名", "品目コード", "単価", "数量", "単位",
    "納品書番号", "詳細", "金額", "品目消費税率",
]

CSV_TYPE = "40101"          # サンプル準拠の固定値
DEFAULT_TAX_RATE = "10%"    # 全品目10%固定


def _round_yen(value):
    """円未満を四捨五入して整数に丸める。"""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calc_totals(items):
    """
    品目リストから 小計・消費税・合計 を算出する。
    items: [{"品名":..., "単価":..., "数量":..., "金額":...}, ...]
    税は明細金額合計に対して最後に一括10%（サンプルで検証済 661482×10%=66148）。
    """
    subtotal = sum(_round_yen(it["金額"]) for it in items)
    tax = _round_yen(subtotal * Decimal("0.10"))
    total = subtotal + tax
    return subtotal, tax, total


def build_rows(header, items):
    """
    header（取引先・件名・各日付・振込先など）と items（品目リスト）から
    MFクラウドCSVの全行（ヘッダ行込みの2次元リスト）を組み立てて返す。
    """
    subtotal, tax, total = calc_totals(items)

    def blank_row():
        return {col: "" for col in MF_COLUMNS}

    rows = []

    # --- 1行目：請求書行（ヘッダ情報） ---
    r = blank_row()
    r["csv_type(変更不可)"] = CSV_TYPE
    r["行形式"] = "請求書"
    r["取引先名称"] = header.get("取引先名称", "")
    r["件名"] = header.get("件名", "")
    r["請求日"] = header.get("請求日", "")
    r["お支払期限"] = header.get("お支払期限", "")
    r["請求書番号"] = header.get("請求書番号", "")
    r["売上計上日"] = header.get("売上計上日", header.get("請求日", ""))
    r["小計"] = subtotal
    r["消費税"] = tax
    r["合計金額"] = total
    r["取引先敬称"] = header.get("取引先敬称", "")
    r["取引先郵便番号"] = header.get("取引先郵便番号", "")
    r["取引先都道府県"] = header.get("取引先都道府県", "")
    r["取引先住所1"] = header.get("取引先住所1", "")
    r["取引先住所2"] = header.get("取引先住所2", "")
    r["取引先部署"] = header.get("取引先部署", "")
    r["取引先担当者役職"] = header.get("取引先担当者役職", "")
    r["取引先担当者氏名"] = header.get("取引先担当者氏名", "")
    r["自社担当者氏名"] = header.get("自社担当者氏名", "")
    r["備考"] = header.get("備考", "")
    r["振込先"] = header.get("振込先", "")
    rows.append(r)

    # --- 2行目以降：品目行 ---
    for it in items:
        r = blank_row()
        r["csv_type(変更不可)"] = CSV_TYPE
        r["行形式"] = "品目"
        r["品名"] = it.get("品名", "")
        r["品目コード"] = it.get("品目コード", "")
        r["単価"] = _round_yen(it.get("単価", 0))
        # 数量は小数あり得る（例 14.25）。整数なら整数表記にする
        qty = it.get("数量", 1)
        r["数量"] = int(qty) if float(qty) == int(float(qty)) else qty
        r["単位"] = it.get("単位", "")
        r["納品書番号"] = it.get("納品書番号", "")
        r["詳細"] = it.get("詳細", "")
        r["金額"] = _round_yen(it.get("金額", 0))
        r["品目消費税率"] = it.get("品目消費税率", DEFAULT_TAX_RATE)
        rows.append(r)

    # 辞書を列順の2次元リストへ
    matrix = [MF_COLUMNS]
    for r in rows:
        matrix.append([r[col] for col in MF_COLUMNS])
    return matrix


def to_csv_bytes(header, items, encoding="utf-8-sig"):
    """
    MFクラウド取込用CSVをbytesで返す。
    encoding: "utf-8-sig"(BOM付きUTF-8) または "cp932"(Shift-JIS) を選択可能。
    """
    matrix = build_rows(header, items)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerows(matrix)
    text = buf.getvalue()
    if encoding == "cp932":
        return text.encode("cp932", errors="replace")
    return text.encode("utf-8-sig")
