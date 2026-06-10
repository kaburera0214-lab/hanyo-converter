# -*- coding: utf-8 -*-
"""
買掛システムの取引先マスタ初期データ(payable_master_seed.csv)を生成するワンタイムツール。

入力(ローカルのDownloads想定):
  - 取引先情報.xlsx 「マスタ」シート … 122社の口座情報(銀行/支店/種目/口座番号/カナ名義/顧客番号)
  - 取引先情報.csv               … 支払メタ(科目/支払方法/支払日/銀行/備考)

出力:
  - リポジトリ直下 payable_master_seed.csv (UTF-8)
    会社名,別名,NE仕入先cd,科目,支払方法,支払日,銀行,銀行番号,支店番号,預金種目,
    口座番号,受取人口座名,顧客番号,固定額,除外フラグ,備考

会社名で2源を突合。Notionへはアプリ(13_取引先マスタ)からこのCSVをseedする。
"""
import csv
import os
import re

DL = os.path.join(os.path.expanduser("~"), "Downloads")
XLSX = os.path.join(DL, "取引先情報.xlsx")
CSV_META = os.path.join(DL, "取引先情報.csv")
OUT = os.path.join(os.path.dirname(__file__), "..", "payable_master_seed.csv")

# 預金種目コード -> 名称
SHUMOKU = {"1": "普通", "2": "当座", "普通": "普通", "当座": "当座"}


def norm_name(s):
    """会社名の表記ゆれを吸収した突合キー。"""
    if not s:
        return ""
    s = str(s).strip()
    # 全角カッコ内の補足(住信SBI 等)・末尾の※注記は突合キーから除外
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"※.*$", "", s)
    s = s.replace("株式会社", "").replace("有限会社", "").replace("合同会社", "")
    s = s.replace(" ", "").replace("　", "").replace("　", "")
    return s.lower()


def load_meta():
    """取引先情報.csv -> {norm会社名: {科目,支払方法,支払日,銀行,備考,会社名}}。"""
    meta = {}
    with open(CSV_META, encoding="utf-8-sig") as fp:
        reader = csv.reader(fp)
        header = next(reader)
        for row in reader:
            if len(row) < 6:
                continue
            kamoku, houhou, biday, ginko, bikou, kaisha = row[0], row[1], row[2], row[3], row[4], row[5]
            kaisha = (kaisha or "").strip()
            if not kaisha:
                continue
            key = norm_name(kaisha)
            if not key:
                continue
            meta[key] = {
                "会社名": kaisha,
                "科目": kamoku.strip(),
                "支払方法": houhou.strip(),
                "支払日": biday.strip(),
                "銀行": ginko.strip(),
                "備考": bikou.strip(),
            }
    return meta


def parse_fixed_amount(bikou):
    """備考の "253000/月" "5500/月" "3719/回" 等から固定額(数値)を抽出。無ければ空。"""
    if not bikou:
        return ""
    m = re.search(r"([0-9][0-9,]*)\s*/\s*[月回年]", bikou)
    if m:
        return m.group(1).replace(",", "")
    return ""


def main():
    import openpyxl
    import sys
    import os as _os
    sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from lib.payable import bank_master as BM

    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    ws = wb["マスタ"]
    meta = load_meta()

    rows = []
    used_meta = set()
    for r in ws.iter_rows(min_row=5, values_only=True):
        # A=No,B=銀行番号,C=支店番号,D=預金種目,E=口座番号,F=カナ名義,G=会社名,I=顧客番号
        kaisha = r[6]
        if not kaisha or not str(kaisha).strip():
            continue
        kaisha = str(kaisha).strip()
        key = norm_name(kaisha)
        m = meta.get(key, {})
        if m:
            used_meta.add(key)
        bikou = m.get("備考", "")
        bank_no = str(r[1]).strip() if r[1] is not None else ""
        branch_no = str(r[2]).strip() if r[2] is not None else ""
        rows.append({
            "会社名": kaisha,
            "別名": "",  # 請求書表記ゆれ用(後で人が追記)
            "NE仕入先cd": "",  # 突合用(後で人が紐付け)
            "科目": m.get("科目", ""),
            "支払方法": m.get("支払方法", ""),
            "支払日": m.get("支払日", ""),
            # 銀行=受取人銀行名(番号から),支店=受取人支店名(番号から)
            "銀行": BM.bank_name(bank_no),
            "支店": BM.branch_name(bank_no, branch_no),
            "銀行番号": bank_no,
            "支店番号": branch_no,
            "預金種目": SHUMOKU.get(str(r[3]).strip(), str(r[3]).strip()) if r[3] is not None else "",
            "口座番号": str(r[4]).strip() if r[4] is not None else "",
            "受取人口座名": str(r[5]).strip() if r[5] is not None else "",
            "顧客番号": str(r[8]).strip() if r[8] is not None else "",
            "固定額": parse_fixed_amount(bikou),
            "除外フラグ": "",
            "支払元銀行": m.get("銀行", ""),  # 弊社の支払元(楽天等)。温存
            "備考": bikou,
        })

    fields = ["会社名", "別名", "NE仕入先cd", "科目", "支払方法", "支払日", "銀行",
              "支店", "銀行番号", "支店番号", "預金種目", "口座番号", "受取人口座名",
              "顧客番号", "固定額", "除外フラグ", "支払元銀行", "備考"]
    with open(OUT, "w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # 突合レポート
    matched = len(used_meta)
    print(f"マスタ口座行: {len(rows)}社")
    print(f"支払メタ突合: {matched}社 / メタ総数 {len(meta)}社")
    only_master = [x["会社名"] for x in rows if norm_name(x["会社名"]) not in meta]
    only_meta = [meta[k]["会社名"] for k in meta if k not in used_meta]
    print(f"\n口座マスタにあるが支払メタ未突合 ({len(only_master)}社):")
    print("  " + " / ".join(only_master[:40]))
    print(f"\n支払メタにあるが口座マスタに無い(口座振替/現金/未登録など) ({len(only_meta)}社):")
    print("  " + " / ".join(only_meta[:60]))
    print(f"\n出力: {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
