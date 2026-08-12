# -*- coding: utf-8 -*-
"""MFクラウド会計 買掛未払CSVの生成テスト。"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.payable import mf_csv  # noqa: E402


def test_取引日と取引Noの既定値():
    """振込実行が2026/08/31 → 取引日は前月末日2026/07/31、取引No 10731。"""
    d = mf_csv.prev_month_end("2026/08/31")
    assert d == datetime.date(2026, 7, 31)
    assert mf_csv.torihiki_no(d) == "10731"       # 5桁(MFは9桁以内)
    assert mf_csv.month_end(2026, 7) == d          # 対象月の末日と一致する


def test_月初の前月末日():
    assert mf_csv.prev_month_end("2026-01-05") == datetime.date(2025, 12, 31)
    assert mf_csv.torihiki_no(datetime.date(2025, 12, 31)) == "11231"


def _rec(name, amount, hojo=None):
    return {"借方勘定科目": "仕入高", "借方補助科目": hojo or name, "借方税区分": "課税仕入 10%",
            "貸方勘定科目": "買掛金", "貸方補助科目": hojo or name, "貸方税区分": "対象外",
            "摘要": name, "金額": amount}


def test_1行目にだけ取引Noと取引日が入る():
    csv_bytes = mf_csv.build_kaikake_csv(
        [_rec("アーテック", 18327), _rec("アテイン", 5753)], "10531", "2026/05/31")
    lines = csv_bytes.decode("utf-8").split("\r\n")
    assert lines[0].startswith("取引No,取引日,借方勘定科目")
    assert lines[1] == ('165,2026/05/31,仕入高,アーテック,課税仕入 10%,,"18,327",'
                        '買掛金,アーテック,対象外,,"18,327",アーテック,,').replace("165", "10531")
    assert lines[2].startswith(",,仕入高,アテイン,")   # 2行目以降は空欄(複合仕訳)


def test_全行に出力するオプション():
    csv_bytes = mf_csv.build_kaikake_csv(
        [_rec("アーテック", 100), _rec("アテイン", 200)], "10531", "2026/05/31",
        every_row=True)
    lines = csv_bytes.decode("utf-8").split("\r\n")
    assert lines[2].startswith("10531,2026/05/31,")


def test_金額はカンマ区切りで引用符付き_赤伝も出る():
    out = mf_csv.build_kaikake_csv([_rec("テスト", -1234)], "10731", "2026/07/31")
    assert '"-1,234"' in out.decode("utf-8")


def test_マスタCSVの読み込み():
    import io
    src = ("取引No,取引日,借方勘定科目,借方補助科目,借方税区分,借方部門,借方金額(円),"
           "貸方勘定科目,貸方補助科目,貸方税区分,貸方部門,貸方金額(円),摘要,タグ,メモ\r\n"
           ",2026/00/31,仕入高,アクセス,課税仕入 10%,,,買掛金,アクセス,対象外,,,アクセス,,\r\n"
           ",,,,,,,,,,,,エランド,,\r\n")
    rows = mf_csv.read_mf_master_csv(io.BytesIO(src.encode("utf-8")))
    assert len(rows) == 2
    assert rows[0]["摘要"] == "アクセス" and rows[0]["借方勘定科目"] == "仕入高"
    assert rows[0]["MF並び順"] == 1
    assert rows[1]["摘要"] == "エランド" and rows[1]["借方勘定科目"] == ""   # 未設定
