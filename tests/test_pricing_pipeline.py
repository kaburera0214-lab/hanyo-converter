# -*- coding: utf-8 -*-
"""
価格改定パイプラインのエンドツーエンドテスト（CSV入力→突合→計算→出力CSV）。
実行: hanyo-converter直下で  python tests/test_pricing_pipeline.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd  # noqa: E402

from lib.pricing import calc, export as ex, masters, pipeline  # noqa: E402

P = dict(calc.DEFAULT_PARAMS)


def _ne_master():
    csv = (
        "商品コード,商品名,JANコード,売価,原価,項目1\n"
        "miya0284,テスト宮商品,4900000000284,6500,5100,60\n"
        "kwgc0414,テストKWGC,4900000000414,1600,900,yuup3\n"
        "artc9999,テスト値下げ,4900000009999,2000,1500,80\n"
        "slvb0144,テストサイズ変更,4900000000144,800,480,yuup3\n"
    ).encode("cp932")
    df, missing = masters.load_ne_master(csv)
    assert missing == []
    return masters.build_lookup(df)


def _cost_table():
    return masters.cost_lookup(masters.load_cost_master_bundled())


def test_tab1_end_to_end():
    jan_map, code_info = _ne_master()
    cost_table = _cost_table()
    in_df = pd.DataFrame([
        {"JAN": "4900000000284", "新下代": "5200"},   # 値上げ 5100→5200
        {"JAN": "4900000000414", "新下代": "1026"},   # 値上げ 900→1026
        {"JAN": "4900000009999", "新下代": "1200"},   # 値下げ→据え置き
        {"JAN": "4999999999999", "新下代": "500"},    # 未マッチ
    ])
    c_jan = pipeline.pick_col(in_df, "JANコード", "JAN")
    c_cost = pipeline.pick_col(in_df, "新下代", "下代")
    matched, unmatched = pipeline.match_input(in_df, None, c_jan, jan_map, code_info)
    assert len(matched) == 3 and unmatched == ["JAN 4999999999999"]

    rows = pipeline.build_price_rows(matched, c_cost, cost_table, P)
    by_code = {r["商品コード"]: r for r in rows}

    # miya0284: 現価格=6500*1.1=7150、値上げ率価格=7150*5200/5100=7290 < 9103 → 20%価格
    r = by_code["miya0284"]
    assert r["現販売価格"] == 7150
    assert r["新販売価格"] == 9103 and r["NE売価"] == 8275
    assert r["新利益額"] == 1534

    # kwgc0414: 現価格=1600*1.1=1760、20%価格2054 vs 値上げ率 1760*1026/900=2006 → 2054
    r = by_code["kwgc0414"]
    assert r["現販売価格"] == 1760 and r["新販売価格"] == 2054

    # artc9999: 値下げ → 据え置き（現価格2200のまま）
    r = by_code["artc9999"]
    assert r["新販売価格"] == r["現販売価格"] == 2200
    assert "据え置き" in r["適用ルール"]

    # 出力CSV（実際のアップロード実績ファイルの形式）
    ok = [r for r in rows if r["新販売価格"]]
    changed = [r for r in ok if r["新販売価格"] != r["現販売価格"]]
    mall = [{"商品コード": r["商品コード"], "楽天販売価格": r["新販売価格"],
             "Yahoo販売価格": r["新販売価格"]} for r in changed]
    rak_records, rak_missing = ex.rakuten_rows(mall, {})  # 対応表なし＝枝番なしは単品扱い
    assert rak_missing == []
    rak = ex.rakuten_csv(rak_records).decode("cp932")
    assert "商品管理番号（商品URL）,商品番号,SKU管理番号,システム連携用SKU番号,販売価格,表示価格" in rak
    assert "miya0284,miya0284,,,," in rak            # 親行
    assert "miya0284,,miya0284,,9103,9103" in rak    # 単品SKU行
    assert "artc9999" not in rak                     # 据え置きは含まない
    yah_records, yah_diff = ex.yahoo_rows(mall, {})
    yah = ex.yahoo_csv(yah_records).decode("cp932")
    assert "code,price" in yah and "kwgc0414,2054" in yah and yah_diff == []
    ne = ex.ne_csv([{"商品コード": r["商品コード"], "NE売価": r["NE売価"],
                     "NE原価": r["新下代"]} for r in ok]).decode("cp932")
    assert "syohin_code,baika_tnk,genka_tnk" in ne
    assert "miya0284,8275,5200" in ne
    assert "artc9999,2000,1200" in ne  # 据え置きでも原価は更新


def test_tab2_direct_end_to_end():
    jan_map, code_info = _ne_master()
    in_df = pd.DataFrame([{"JAN": "4900000000284", "新下代": "5200", "新送料": "1000"}])
    c_jan = pipeline.pick_col(in_df, "JAN")
    matched, unmatched = pipeline.match_input(in_df, None, c_jan, jan_map, code_info)
    rows = pipeline.build_price_rows(matched, "新下代", {}, P, mode="direct", c_ship="新送料")
    # Q=(1000+0+715+5200)*1.1=7606.5 → 20%価格9508 vs 値上げ率7290 → 9508
    assert rows[0]["新販売価格"] == 9508


def test_tab3_size_change_end_to_end():
    jan_map, code_info = _ne_master()
    cost_table = _cost_table()
    in_df = pd.DataFrame([
        {"JAN": "4900000000144", "新項目1": "60", "楽天販売価格": "880"},   # メール便→宅配便
        {"JAN": "4900000000414", "新項目1": "nekop", "楽天販売価格": ""},   # メール便同士
    ])
    matched, _ = pipeline.match_input(in_df, None, "JAN", jan_map, code_info)
    rows = pipeline.size_change_rows(matched, "新項目1", "楽天販売価格", cost_table, P)
    by_code = {r["商品コード"]: r for r in rows}

    r = by_code["slvb0144"]  # 880円のまま60サイズ宅配便になると利益NG・配送設定要修正
    assert r["価格チェック"] == "〇"          # 800*1.1=880
    assert r["配送設定"] == "要修正"
    assert r["利益チェック"] == "×"

    r = by_code["kwgc0414"]  # yuup3→nekop（メール便同士・送料下がる）
    assert r["配送設定"] == "不要"
    assert r["利益チェック"] == "〇"
    assert r["価格チェック"] == "-"           # 楽天価格未提供

    item1 = ex.ne_item1_csv([{"商品コード": r["商品コード"], "新項目1": r["新項目1"]}
                             for r in rows]).decode("cp932")
    assert "slvb0144,60" in item1 and "kwgc0414,nekop" in item1


def test_rakuten_sku_master_and_export():
    """RMS商品一括DL → SKU対応表 → normal-item.csv（実物2024-01-16 keiの構造を再現）"""
    rms = (
        "商品管理番号（商品URL）,商品番号,SKU管理番号,システム連携用SKU番号,販売価格,表示価格\n"
        "kei0001,kei0001,,,,\n"
        "kei0001,,8577,kei0001-01,2530,2530\n"
        "kei0001,,8578,kei0001-02,2530,2530\n"
        "kei0018,kei0018,,,,\n"
        "kei0018,,kei0018,,2530,2530\n"
    ).encode("cp932")
    sku_df = masters.parse_rakuten_item_csv(rms)
    table = masters.sku_lookup(sku_df)
    assert table["kei0001-01"] == ("kei0001", "8577", "kei0001-01")
    assert table["kei0018"] == ("kei0018", "kei0018", "")

    mall = [
        {"商品コード": "kei0001-01", "楽天販売価格": 2783, "Yahoo販売価格": 2783},
        {"商品コード": "kei0001-02", "楽天販売価格": 2530, "Yahoo販売価格": 2530},
        {"商品コード": "kei0018", "楽天販売価格": 2783, "Yahoo販売価格": 2783},
        {"商品コード": "zzz0001-01", "楽天販売価格": 999, "Yahoo販売価格": 999},  # 対応表に無い枝番付き
    ]
    records, missing = ex.rakuten_rows(mall, table)
    assert missing == ["zzz0001-01"]
    rak = ex.rakuten_csv(records).decode("cp932")
    assert "kei0001,kei0001,,,," in rak                    # 親行
    assert "kei0001,,8577,kei0001-01,2783,2783" in rak     # SKU行（楽天採番）
    assert "kei0001,,8578,kei0001-02,2530,2530" in rak
    assert "kei0018,,kei0018,,2783,2783" in rak            # 単品
    assert "zzz0001" not in rak

    yah_records, diff = ex.yahoo_rows(mall, table)
    yah = {r["code"]: r["price"] for r in yah_records}
    assert yah["kei0001"] == 2783        # SKUで割れたら最高値
    assert yah["kei0018"] == 2783
    assert yah["zzz0001"] == 999         # Yahooは枝番を落として親コードで出せる
    assert diff == ["kei0001"]


def test_overrides_and_force():
    """手修正・据え置き外し（サイズ変更由来の再設定）"""
    jan_map, code_info = _ne_master()
    cost_table = _cost_table()
    in_df = pd.DataFrame([{"商品コード": "artc9999", "新下代": "1200"}])
    matched, _ = pipeline.match_input(in_df, "商品コード", None, jan_map, code_info)
    # force_reprice: 値下げでも20%価格に再設定される
    rows = pipeline.build_price_rows(matched, "新下代", cost_table, P, force_reprice=True)
    assert rows[0]["新販売価格"] != rows[0]["現販売価格"]
    # 手修正が最優先
    rows = pipeline.build_price_rows(matched, "新下代", cost_table, P,
                                     overrides={"artc9999": 3300})
    assert rows[0]["新販売価格"] == 3300 and rows[0]["適用ルール"] == "手修正"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except AssertionError as e:
                fails += 1
                import traceback
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)
