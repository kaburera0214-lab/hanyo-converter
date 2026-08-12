# -*- coding: utf-8 -*-
"""買掛の突合（NE発注データ×請求書）の名寄せ回帰テスト。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.payable import matching as M  # noqa: E402


def _agg(rows):
    return M.aggregate_ne(rows, 2026, 7)


def _ne(cd, name, amount, tokki="", denpyo="1"):
    return {"発注伝票番号": denpyo, "仕入先cd": cd, "仕入先名": name, "特記事項": tokki,
            "金額": amount, "作成日": "2026/07/13", "状態": "仕入完了"}


def test_カッコ内の別称でNE仕入先と紐づく():
    """NE『株式会社 加藤製本（クルーシャル）』⇔ マスタ『クルーシャル』。"""
    ne = _agg([_ne("c002", "【W17/M2】株式会社 加藤製本（クルーシャル）【1000】",
                   "2,238", "200円送料")])
    look = M.build_master_lookup([{"会社名": "クルーシャル", "別名": "", "NE仕入先cd": ""}])
    r = M.match_invoice("クルーシャル", 2438, look, ne)
    assert r["状態"] == "一致"
    assert r["NE仕入先cd"] == "c002"      # 名称一致で判明したcd(マスタ登録用)
    assert r["紐付け方法"] == "名称"


def test_仕入先cdが入っていればそちらを優先():
    ne = _agg([_ne("c002", "【W17/M2】株式会社 加藤製本【1000】", "2,438")])
    look = M.build_master_lookup([{"会社名": "クルーシャル", "別名": "", "NE仕入先cd": "c002"}])
    r = M.match_invoice("クルーシャル", 2438, look, ne)
    assert r["状態"] == "一致"
    assert r["紐付け方法"] == "仕入先cd"


def test_短いカッコ注記はキーにしない():
    """『トヨタ（A）』の『A』のような注記で誤紐付けしない。"""
    assert "a" not in M.name_keys("トヨタ（A）")
    assert "クルーシャル" in M.name_keys("株式会社 加藤製本（クルーシャル）")


def test_注記なしの本体行を優先して引く():
    """『野中製作所』と『野中製作所(コンテナ30％)』は正規化が同じキーになる。"""
    look = M.build_master_lookup([
        {"会社名": "野中製作所", "別名": "", "NE仕入先cd": "", "口座番号": "262424"},
        {"会社名": "野中製作所(コンテナ30％)", "別名": "", "NE仕入先cd": "", "口座番号": ""},
    ])
    assert look["by_norm"][M.normalize_name("野中製作所")]["口座番号"] == "262424"


def test_発注が無ければ発注なし():
    ne = _agg([_ne("n001", "【Z11/M2】株式会社野中製作所【0】", "1,000")])
    look = M.build_master_lookup([{"会社名": "カミイソ産商", "別名": "", "NE仕入先cd": ""}])
    r = M.match_invoice("カミイソ産商", 3050, look, ne)
    assert r["状態"] == "発注なし"


def test_似た仕入先を候補として提示できる():
    ne = _agg([_ne("k008", "【F12/M3】カミイソ株式会社【980】", "3,050")])
    cands = M.find_ne_candidates("カミイソ産商", ne, {"会社名": "カミイソ産商", "別名": ""})
    assert cands and cands[0][0] == "k008"
