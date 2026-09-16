# -*- coding: utf-8 -*-
"""
MFクラウド会計 総合振込仕訳帳CSV（②）の生成テスト。

要は「経理の現物と1バイトも違わないものが出るか」を見張るテスト。

tests/fixtures/総合振込仕訳帳_sample.csv は、2026-08-12 に経理から受け取った実物
（Googleスプレッドシートからの書き出し）から、**列構成・行数・空欄の位置・引用符・
金額の桁数・源泉行の位置をそのまま残し、取引先名と金額だけ差し替えた**もの。
このリポジトリは public なので、取引先名・金額・振込カナ名はそのまま置けない。
現物はローカルのダウンロードフォルダ「支払いまとめ_経理用 - 総合振込仕訳帳 (1).csv」。

列の増減・引用符・空欄の数が変わったら落ちる。
"""
import csv
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.payable import mf_csv  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "総合振込仕訳帳_sample.csv")


def _fixture_bytes():
    with open(FIXTURE, "rb") as fp:
        return fp.read()


def _fixture_rows():
    return list(csv.reader(io.StringIO(_fixture_bytes().decode("utf-8"), newline="")))


def _records_from_fixture():
    """
    サンプルCSVから、②の入力（振込対象レコード）を復元する。

    借方金額は「振込額＋源泉税額」なので、末尾の預り金行と摘要で突き合わせて
    源泉分を差し引き、実際の振込額に戻す（＝楽天総合振込CSVに出る額）。
    """
    rows = _fixture_rows()[1:]          # ヘッダを除く
    gensen = {r[12]: mf_csv.to_int(r[11]) for r in rows
              if r[7] == mf_csv.GENSEN_KASHIKATA["勘定科目"]}
    records = []
    for r in rows:
        if not r[2]:                     # 借方が空＝1行目の仮受金 or 末尾の預り金
            continue
        tekiyo = r[12]
        g = gensen.get(tekiyo, 0)
        records.append({
            "借方勘定科目": r[2], "借方補助科目": r[3], "借方税区分": r[4],
            "摘要": tekiyo, "金額": mf_csv.to_int(r[6]) - g, "源泉税額": g,
        })
    return records


def test_受領サンプルと1バイト単位で一致する():
    """列・行構成・カンマ・引用符までサンプルと同じか。末尾改行だけは事情があり別扱い。"""
    rows = _fixture_rows()
    out = mf_csv.build_soufurikomi_csv(_records_from_fixture(),
                                       rows[1][0], rows[1][1])
    # 元がスプレッドシートの書き出しなので最終行に改行が無い。そこだけ補って比較する
    assert out == _fixture_bytes() + b"\r\n"


def test_サンプルは14列でメモ列が無い():
    """①は15列・②は14列。ここを取り違えるとMFの取込で列がずれる。"""
    assert _fixture_rows()[0] == mf_csv.SOUFURIKOMI_HEADER
    assert len(mf_csv.SOUFURIKOMI_HEADER) == 14
    assert "メモ" not in mf_csv.SOUFURIKOMI_HEADER
    assert "メモ" in mf_csv.KAIKAKE_HEADER


def test_サンプルの貸借が一致している():
    """テストの前提（サンプルが正しい仕訳であること）自体を確かめる。"""
    rows = _fixture_rows()[1:]
    assert sum(mf_csv.to_int(r[6]) for r in rows) == sum(mf_csv.to_int(r[11]) for r in rows)


def _rec(name, amount, kamoku="買掛金", gensen=0):
    return {"借方勘定科目": kamoku, "借方補助科目": name, "借方税区分": "対象外",
            "摘要": name, "金額": amount, "源泉税額": gensen}


def test_1行目は仮受金の振込総額で摘要は総合振込():
    out = mf_csv.build_soufurikomi_csv(
        [_rec("取引先イ", 21622), _rec("取引先ウ", 3949)], "164", "2026/06/30")
    lines = out.decode("utf-8").split("\r\n")
    assert lines[1] == '164,2026/06/30,,,,,,仮受金,,対象外,,"25,571",総合振込,'
    assert lines[2] == ',,買掛金,取引先イ,対象外,,"21,622",,,,,,取引先イ,'


def test_源泉がある取引先は借方に足し戻して貸方に預り金行が立つ():
    """振込は源泉を引いた額で行うので、借方（費用）は振込額より大きくなる。"""
    out = mf_csv.build_soufurikomi_csv(
        [_rec("取引先イ", 21622),
         _rec("シヤロウシ", 9979, kamoku="支払報酬", gensen=1021)],
        "164", "2026/06/30")
    lines = out.decode("utf-8").split("\r\n")
    assert '"31,601"' in lines[1]                     # 仮受金＝振込総額（源泉は含まない）
    assert '支払報酬,シヤロウシ,対象外,,"11,000"' in lines[3]   # 借方＝振込額＋源泉
    assert lines[4] == ',,,,,,,預り金,所得税,対象外,,"1,021",シヤロウシ,'


def test_勘定科目が抜けたレコードは生成せずに落ちる():
    """
    科目なしの行をMFに取り込ませない。取り込めてしまうと、科目の無い仕訳が1本
    静かに増えるだけで、画面にもMFにも「おかしい」とは出ない。
    """
    recs = [_rec("取引先イ", 21622), {"摘要": "科目未設定商店", "金額": 500}]
    with pytest.raises(ValueError, match="科目未設定商店"):
        mf_csv.build_soufurikomi_csv(recs, "164", "2026/06/30")


def test_貸借は組み立て方で必ず一致する():
    """借方＝振込額＋源泉／貸方＝仮受金＋預り金。源泉が混ざっても崩れないことを確かめる。"""
    out = mf_csv.build_soufurikomi_csv(
        [_rec("取引先イ", 21622),
         _rec("シヤロウシ", 9979, kamoku="支払報酬", gensen=1021),
         _rec("取引先エ", 100148)], "164", "2026/06/30")
    rows = list(csv.reader(io.StringIO(out.decode("utf-8"), newline="")))[1:]
    assert sum(mf_csv.to_int(r[6]) for r in rows) == sum(mf_csv.to_int(r[11]) for r in rows)


def test_取引日は対象月の翌月の振込実行日():
    """①は対象月の末日、②は振込実行日。基準日が1ヶ月ずれる（現物もこの関係だった）。"""
    assert mf_csv.furikomi_date("2026-05", "0630") == "2026/06/30"
    assert mf_csv.month_end(2026, 5).strftime("%Y/%m/%d") == "2026/05/31"


def test_年をまたぐ振込実行日():
    """12月分を1月に振り込むと年が変わる。ここを間違えると1年ずれた仕訳が入る。"""
    assert mf_csv.furikomi_date("2025-12", "0130") == "2026/01/30"
    assert mf_csv.furikomi_date("2026-01", "0227") == "2026/02/27"


def test_借方科目はマスタの貸方列を反転して使う():
    """②の借方＝①の貸方。計上済みの買掛金・未払金を取り崩す形。"""
    m = {"借方勘定科目": "仕入高", "借方補助科目": "取引先ア", "借方税区分": "課税仕入 10%",
         "貸方勘定科目": "買掛金", "貸方補助科目": "取引先ア", "貸方税区分": "対象外"}
    assert mf_csv.soufurikomi_karikata(m) == {
        "借方勘定科目": "買掛金", "借方補助科目": "取引先ア", "借方税区分": "対象外"}


def test_計上していない取引先は借方列をそのまま使う():
    """貸方（買掛金・未払金）が無い＝支払時に費用計上する型。社労士の支払報酬など。"""
    m = {"借方勘定科目": "支払報酬", "借方補助科目": "", "借方税区分": "課税仕入 10%",
         "貸方勘定科目": "", "貸方補助科目": "", "貸方税区分": ""}
    assert mf_csv.soufurikomi_karikata(m) == {
        "借方勘定科目": "支払報酬", "借方補助科目": "", "借方税区分": "課税仕入 10%"}


def test_勘定科目が未設定ならNoneを返す():
    """呼び出し側で『マスタに勘定科目が未設定』として画面に出す。黙って0円で通さない。"""
    assert mf_csv.soufurikomi_karikata({"借方勘定科目": "", "貸方勘定科目": ""}) is None


def _transfer(name, amount, master):
    return {"会社名": name, "金額": amount, "_master": dict(master, 会社名=name)}


def test_振込対象からマスタを引いて仕訳レコードを組む():
    """摘要は受取人口座名。借方はマスタの貸方列。源泉はマスタの固定値。"""
    recs, skipped = mf_csv.build_soufurikomi_records([
        _transfer("取引先ア", 76038, {
            "借方勘定科目": "仕入高", "借方補助科目": "取引先ア", "借方税区分": "課税仕入 10%",
            "貸方勘定科目": "買掛金", "貸方補助科目": "取引先ア", "貸方税区分": "対象外",
            "摘要": "取引先ア", "受取人口座名": "カ）トリヒキサキア"}),
    ])
    assert skipped == []
    assert recs[0] == {"借方勘定科目": "買掛金", "借方補助科目": "取引先ア",
                       "借方税区分": "対象外", "摘要": "カ）トリヒキサキア",
                       "金額": 76038, "源泉税額": 0, "会社名": "取引先ア"}


def test_受取人口座名が空なら摘要_会社名の順に落とす():
    recs, _ = mf_csv.build_soufurikomi_records([
        _transfer("取引先ウ", 3949, {"貸方勘定科目": "買掛金", "貸方補助科目": "取引先ウ",
                                  "貸方税区分": "対象外", "摘要": "取引先ウ", "受取人口座名": ""}),
        _transfer("名無し", 100, {"貸方勘定科目": "買掛金", "摘要": "", "受取人口座名": ""}),
    ])
    assert [r["摘要"] for r in recs] == ["取引先ウ", "名無し"]


def test_勘定科目が引けない取引先はskippedに出て黙って消えない():
    """
    ここで黙って落とすと、仮受金だけ大きい（＝貸借の合わない）CSVができる。
    画面はこのskippedを出したうえで、仮受金と楽天CSV合計の不一致でDLを止める。
    """
    recs, skipped = mf_csv.build_soufurikomi_records([
        _transfer("取引先イ", 21622, {"貸方勘定科目": "買掛金", "貸方補助科目": "取引先イ",
                                    "貸方税区分": "対象外", "受取人口座名": "取引先イ"}),
        _transfer("未設定商店", 5000, {"借方勘定科目": "", "貸方勘定科目": ""}),
    ])
    assert len(recs) == 1
    assert skipped == [("未設定商店", "マスタに勘定科目が未設定")]


def test_源泉税額はマスタの固定値をそのまま使う():
    recs, _ = mf_csv.build_soufurikomi_records([
        _transfer("社労士オ", 9979, {
            "借方勘定科目": "支払報酬", "借方補助科目": "", "借方税区分": "課税仕入 10%",
            "貸方勘定科目": "", "受取人口座名": "シヤロウシオ",
            "源泉税額": 1021}),
    ])
    assert recs[0]["借方勘定科目"] == "支払報酬"      # 計上していないので借方列を使う
    assert recs[0]["源泉税額"] == 1021
    assert recs[0]["金額"] == 9979                   # 金額は振込額のまま（足し戻しは生成時）
