# -*- coding: utf-8 -*-
"""楽天銀行 総合振込CSVの生成テスト。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.payable import rakuten_csv  # noqa: E402

REC = [{"銀行番号": "1", "支店番号": "545", "預金種目": "普通", "口座番号": "1254490",
        "受取人口座名": "カトウセイホン（カ）　クル－シヤル", "金額": 2681}]


def test_ヘッダ行を出力しない():
    text = rakuten_csv.build_csv_text(REC, "0831")
    assert not text.startswith("サービス区分")
    assert text.split("\r\n")[0].startswith("3,0831,")
    assert len([ln for ln in text.split("\r\n") if ln]) == 1


def test_各項目のゼロ埋めと種目コード():
    row = rakuten_csv.build_csv_text(REC, "831").split("\r\n")[0].split(",")
    assert row[0] == "3"            # サービス区分
    assert row[1] == "0831"         # 実行日 MMDD
    assert row[2] == "0001"         # 銀行番号4桁
    assert row[3] == "545"          # 支店番号3桁
    assert row[4] == "1"            # 普通=1
    assert row[5] == "1254490"      # 口座番号7桁
    assert row[7] == "2681"         # 金額(カンマなし)
    assert row[8] == "0002"         # 顧客番号は0002始まりの連番


def test_必要ならヘッダも出せる():
    text = rakuten_csv.build_csv_text(REC, "0831", include_header=True)
    assert text.startswith("サービス区分,実行日,")


def test_bytesはcp932():
    data = rakuten_csv.build_csv_bytes(REC, "0831")
    assert data.decode("cp932").startswith("3,0831,")
