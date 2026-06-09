# -*- coding: utf-8 -*-
"""
内訳明細書のExcel（.xlsx）マルチシート出力（請求書発行機能専用）。

費目ごとに1シート（保管費／送料／出荷作業費／資材費／汎用作業費／その他）＋
先頭にサマリ（費目別合計・総額）を置く。各明細シートには末尾に合計行を付ける。
"""
import io
import pandas as pd


def _sheet_name(name):
    # Excelのシート名は31文字まで／使用不可文字を除去
    bad = '[]:*?/\\'
    s = "".join(c for c in str(name) if c not in bad)
    return s[:31] or "sheet"


def build_breakdown_excel(summary_rows, detail_sheets):
    """
    summary_rows: [{"費目":..., "金額":...}, ...]（サマリシート用）
    detail_sheets: [(シート名, DataFrame, 金額列名 or None), ...]
       金額列名を指定するとそのシート末尾に合計行を付ける。
    返り値: xlsxのbytes
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # サマリ
        sdf = pd.DataFrame(summary_rows, columns=["費目", "金額"])
        total = sum(int(r.get("金額", 0) or 0) for r in summary_rows)
        sdf = pd.concat(
            [sdf, pd.DataFrame([{"費目": "合計", "金額": total}])],
            ignore_index=True)
        sdf.to_excel(writer, sheet_name="サマリ", index=False)

        # 各明細
        for name, df, amount_col in detail_sheets:
            out = df.copy() if df is not None else pd.DataFrame()
            if amount_col and amount_col in out.columns and len(out):
                tot = pd.to_numeric(out[amount_col], errors="coerce").fillna(0).sum()
                total_row = {c: "" for c in out.columns}
                total_row[out.columns[0]] = "合計"
                total_row[amount_col] = int(round(tot))
                out = pd.concat([out, pd.DataFrame([total_row])], ignore_index=True)
            out.to_excel(writer, sheet_name=_sheet_name(name), index=False)

    # 列幅の簡易自動調整
    buf.seek(0)
    from openpyxl import load_workbook
    wb = load_workbook(buf)
    for ws in wb.worksheets:
        for col in ws.columns:
            width = 10
            for cell in col:
                if cell.value is not None:
                    width = max(width, min(50, len(str(cell.value)) + 2))
            ws.column_dimensions[col[0].column_letter].width = width
    out_buf = io.BytesIO()
    wb.save(out_buf)
    return out_buf.getvalue()
