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
    # 売価はNEで管理していないため列自体が無い（現販売価格は楽天から取得する運用）
    csv = (
        "商品コード,商品名,JANコード,原価,項目1\n"
        "miya0284,テスト宮商品,4900000000284,5100,60\n"
        "kwgc0414,テストKWGC,4900000000414,900,yuup3\n"
        "artc9999,テスト値下げ,4900000009999,1500,80\n"
        "slvb0144,テストサイズ変更,4900000000144,480,yuup3\n"
    ).encode("cp932")
    df, missing = masters.load_ne_master(csv)
    assert missing == []
    return masters.build_lookup(df)


# 楽天から取得した現在販売価格（テスト用の固定値）
RK_PRICES = {"miya0284": 7150, "kwgc0414": 1760, "artc9999": 2200, "slvb0144": 880}


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

    rows = pipeline.build_price_rows(matched, c_cost, cost_table, P, cur_prices=RK_PRICES)
    by_code = {r["商品コード"]: r for r in rows}

    # miya0284: 3980以上・値上げ率価格7290 < 目標利益率価格8778 → 8778
    r = by_code["miya0284"]
    assert r["現販売価格"] == 7150
    assert r["新販売価格"] == 8778 and r["NE売価"] == 7980
    assert r["適用ルール"] == "目標利益率価格"
    assert r["新利益額"] == 1316 and abs(r["新利益率"] - 0.15) < 0.01

    # kwgc0414: 値上げ率価格 1760×1026/900=2006（送料は掛けない）> 目標1609
    r = by_code["kwgc0414"]
    assert r["現販売価格"] == 1760 and r["新販売価格"] == 2006
    assert r["適用ルール"] == "値上げ率価格"

    # artc9999: 値下げ（1500→1200）でも高い方ルール。目標15%価格2148 > 値上げ率価格1584 → 2148
    r = by_code["artc9999"]
    assert r["現販売価格"] == 2200 and r["新販売価格"] == 2148  # 値下げ方向もあり得る
    assert r["適用ルール"] == "目標利益率価格"

    # 出力CSV（実際のアップロード実績ファイルの形式）
    ok = [r for r in rows if r["新販売価格"]]
    changed = [r for r in ok if r["新販売価格"] != r["現販売価格"]]
    mall = [{"商品コード": r["商品コード"], "楽天販売価格": r["新販売価格"],
             "Yahoo販売価格": r["新販売価格"]} for r in changed]
    rak_records, rak_missing = ex.rakuten_rows(mall, {})  # 対応表なし＝枝番なしは単品扱い
    assert rak_missing == []
    rak = ex.rakuten_csv(rak_records).decode("cp932")
    assert ("商品管理番号（商品URL）,商品番号,SKU管理番号,システム連携用SKU番号,"
            "販売価格,表示価格,二重価格文言管理番号") in rak
    assert "miya0284,miya0284,,,,," in rak             # 親行（二重価格欄も空）
    assert "miya0284,,miya0284,,8778,8778,1" in rak    # SKU行は二重価格文言管理番号=1固定
    assert "artc9999,,artc9999,,2148,2148,1" in rak    # 値下げ方向の変更も出力される
    yah_records, yah_diff = ex.yahoo_rows(mall, {})
    yah = ex.yahoo_csv(yah_records).decode("cp932")
    assert "code,price" in yah and "kwgc0414,2006" in yah and yah_diff == []
    ne = ex.ne_csv([{"商品コード": r["商品コード"], "NE売価": r["NE売価"],
                     "NE原価": r["新下代"]} for r in ok]).decode("cp932")
    assert "syohin_code,baika_tnk,genka_tnk" in ne
    assert "miya0284,7980,5200" in ne
    assert "artc9999,1953,1200" in ne  # NE売価=2148÷1.1


def test_tab2_direct_end_to_end():
    jan_map, code_info = _ne_master()
    in_df = pd.DataFrame([{"JAN": "4900000000284", "新下代": "5200", "新送料": "1000"}])
    c_jan = pipeline.pick_col(in_df, "JAN")
    matched, unmatched = pipeline.match_input(in_df, None, c_jan, jan_map, code_info)
    rows = pipeline.build_price_rows(matched, "新下代", {}, P, mode="direct", c_ship="新送料",
                                     cur_prices=RK_PRICES)
    # 直送: M=価格そのまま。目標15%価格=(1000+5200)*1.1/0.74=9216 vs 値上げ率7290 → 9216
    assert rows[0]["新販売価格"] == 9216


def test_tab_separated_input_and_code_in_jan_column():
    """実障害の再現（2026-07-17 0717.csv）: タブ区切り入力＋JAN列に商品コードが入っていても通る"""
    from lib.invoice import csv_import
    tsv = "JAN\t新下代\t送料\nartc9999\t1200\t2000\n".encode("cp932")
    df = csv_import.read_csv_auto(tsv)
    assert list(df.columns) == ["JAN", "新下代", "送料"]  # タブ区切りを自動判定
    assert df.iloc[0]["JAN"] == "artc9999"

    jan_map, code_info = _ne_master()
    matched, unmatched = pipeline.match_input(df, None, "JAN", jan_map, code_info)
    assert unmatched == []                                # 商品コードでも突合できる
    assert matched[0][1]["商品コード"] == "artc9999"


def test_free_shipping_flag_for_direct():
    """直送タブの楽天・YahooCSVには送料無料フラグ列が付く（納品タブには付かない）"""
    mall = [{"商品コード": "kei0018", "楽天販売価格": 5000, "Yahoo販売価格": 5000}]
    records, _ = ex.rakuten_rows(mall, {})
    rak = ex.rakuten_csv(records, free_shipping=True).decode("cp932")
    assert rak.splitlines()[0].endswith("二重価格文言管理番号,送料")
    assert "kei0018,kei0018,,,,,,1" in rak            # 商品行に送料=1（送料無料。0は送料別）
    assert "kei0018,,kei0018,,5000,5000,1," in rak    # SKU行はフラグ空欄
    rak2 = ex.rakuten_csv(records).decode("cp932")
    assert "送料" not in rak2.splitlines()[0].replace("送料込", "")  # 納品タブは列なし

    yah_records, _ = ex.yahoo_rows(mall, {})
    yah = ex.yahoo_csv(yah_records, free_shipping=True).decode("cp932")
    assert yah.splitlines()[0] == "code,price,postage-set"
    assert "kei0018,5000,1" in yah
    yah2 = ex.yahoo_csv(yah_records).decode("cp932")
    assert yah2.splitlines()[0] == "code,price"


def test_tab3_size_change_end_to_end():
    """2026-07-17確定フロー: アップ/ダウン分岐→便種変更→利益チェック→NGは価格再設定"""
    jan_map, code_info = _ne_master()
    cost_table = _cost_table()
    in_df = pd.DataFrame([
        {"JAN": "4900000000144", "新項目1": "60", "楽天販売価格": "500"},   # アップ＋便種変更＋利益NG
        {"JAN": "4900000000414", "新項目1": "60", "楽天販売価格": ""},      # アップ＋便種変更・利益OK
        {"JAN": "4900000009999", "新項目1": "60", "楽天販売価格": ""},      # ダウン（80→60・宅配同士）
    ])
    matched, _ = pipeline.match_input(in_df, None, "JAN", jan_map, code_info)
    rows = pipeline.size_change_rows(matched, "新項目1", "楽天販売価格", cost_table, P,
                                     cur_prices=RK_PRICES)
    by_code = {r["商品コード"]: r for r in rows}

    # slvb0144: yuup3(292円)→60(705.5円)=サイズアップ・メール便→宅配便で配送設定要修正
    # 現価格500円では利益NG → 目標15%価格882円に再設定（NE売価802円）
    r = by_code["slvb0144"]
    assert r["区分"] == "サイズアップ"
    assert r["配送設定"] == "要修正（メール便→宅配便）"
    assert r["利益チェック"] == "×"
    assert r["新販売価格"] == 882 and r["NE売価"] == 802
    assert abs(r["新利益率"] - 0.15) < 0.01

    # kwgc0414: yuup3→60=サイズアップ・便種変更あり・現価格1760円なら利益率25%で合格→価格そのまま
    r = by_code["kwgc0414"]
    assert r["区分"] == "サイズアップ"
    assert r["配送設定"] == "要修正（メール便→宅配便）"
    assert r["利益チェック"] == "〇" and r["新販売価格"] is None

    # artc9999: 80(837円)→60(705.5円)=サイズダウン・宅配同士→修正不要・利益チェック無し
    r = by_code["artc9999"]
    assert r["区分"] == "サイズダウン"
    assert r["配送設定"] == "不要"
    assert r["利益チェック"] == "-" and r["新販売価格"] is None

    # 出力CSV: NE項目1更新・配送設定修正（楽天リスト/Yahoo NT・NM）
    item1 = ex.ne_item1_csv([{"商品コード": r["商品コード"], "新項目1": r["新項目1"]}
                             for r in rows]).decode("cp932")
    assert "slvb0144,60" in item1 and "kwgc0414,60" in item1
    dv = [{"商品管理番号": "slvb0144", "商品コード": "slvb0144",
           "旧便種": "メール便", "新便種": "宅配便"}]
    rak = ex.rakuten_delivery_csv(dv).decode("cp932")
    assert "slvb0144,宅配便のみ,メール便→宅配便,slvb0144" in rak
    yah = ex.yahoo_delivery_csv(dv).decode("cp932")
    assert "code,配送グループ管理番号" in yah and "slvb0144,NT" in yah


def test_rakuten_sku_master_and_export():
    """SKU対応表（RMS APIレスポンス由来）→ 保存/復元 → normal-item.csv（実物の構造を再現）"""
    from lib.pricing import rakuten_price
    # RMS Item API 2.0 の variants からSKU対応表を構築（kei0001=2SKU, kei0018=単品）
    info = rakuten_price.match_variants(
        ["kei0001-01", "kei0001-02"], "kei0001",
        {"8577": {"merchantDefinedSkuId": "kei0001-01", "standardPrice": 2530},
         "8578": {"merchantDefinedSkuId": "kei0001-02", "standardPrice": 2530}})
    info.update(rakuten_price.match_variants(
        ["kei0018"], "kei0018", {"kei0018": {"standardPrice": 2530}}))
    table = rakuten_price.to_sku_table(info)
    assert table["kei0001-01"] == ("kei0001", "8577", "kei0001-01")
    assert table["kei0018"] == ("kei0018", "kei0018", "")
    assert rakuten_price.to_prices(info)["kei0001-01"] == 2530

    # Drive保存形式との往復（df→dict）が崩れないこと
    df = masters.sku_table_to_df(table)
    assert masters.sku_lookup(df) == table

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


def test_rakuten_price_required():
    """現販売価格は楽天から取得したものだけを使う（未取得は計算不可・NE売価での代用なし）"""
    jan_map, code_info = _ne_master()
    cost_table = _cost_table()
    in_df = pd.DataFrame([{"商品コード": "miya0284", "新下代": "5200"}])
    matched, _ = pipeline.match_input(in_df, "商品コード", None, jan_map, code_info)
    # 取得済みなら計算できる
    rows = pipeline.build_price_rows(matched, "新下代", cost_table, P,
                                     cur_prices={"miya0284": 7150})
    assert rows[0]["現販売価格"] == 7150 and rows[0]["新販売価格"] == 8778
    # 未取得は計算不可（フォールバックしない）
    rows2 = pipeline.build_price_rows(matched, "新下代", cost_table, P)
    assert rows2[0]["新販売価格"] is None
    assert rows2[0]["適用ルール"] == "計算不可"
    assert "未取得" in rows2[0]["警告"]


def test_parent_code_strips_all_numeric_suffixes():
    """実障害の再現（2026-07-17 gais0020-01-06）: 枝番が2段でも全部除去して親を推定"""
    assert masters.parent_code("gais0020-01-06") == "gais0020"
    assert masters.parent_code("kei0001-01") == "kei0001"
    assert masters.parent_code("marg0037-01-03") == "marg0037"
    assert masters.parent_code("kei0018") == "kei0018"          # 枝番なしはそのまま
    assert masters.parent_code("wauyuu-v3-1478") == "wauyuu-v3"  # 数字でない区切りは残す


def test_match_variants():
    """variantsとNEコードの照合（連携番号一致→SKU番号一致→単一SKU）"""
    from lib.pricing import rakuten_price
    variants = {
        "8577": {"merchantDefinedSkuId": "kei0001-01", "standardPrice": 2530},
        "8578": {"merchantDefinedSkuId": "kei0001-02", "standardPrice": 2640},
    }
    out = rakuten_price.match_variants(["kei0001-01", "kei0001-02", "kei0001-99"],
                                       "kei0001", variants)
    assert out["kei0001-01"]["sku"] == "8577" and out["kei0001-01"]["price"] == 2530
    assert out["kei0001-02"]["price"] == 2640
    assert "kei0001-99" not in out  # 該当SKUなし→呼び出し側で再試行/エラー
    # 単一SKU・連携番号なし（単品）はコード=親で拾える
    out2 = rakuten_price.match_variants(["kei0018"], "kei0018",
                                        {"kei0018": {"standardPrice": 2783}})
    assert out2["kei0018"] == {"parent": "kei0018", "sku": "kei0018",
                               "renkei": "", "price": 2783}
    # SKU管理番号がNEコードと同じ命名の店舗パターン
    out3 = rakuten_price.match_variants(["abc0001-01"], "abc0001",
                                        {"abc0001-01": {"standardPrice": 500}})
    assert out3["abc0001-01"]["sku"] == "abc0001-01"


def test_overrides():
    """手修正が最優先"""
    jan_map, code_info = _ne_master()
    cost_table = _cost_table()
    in_df = pd.DataFrame([{"商品コード": "artc9999", "新下代": "1200"}])
    matched, _ = pipeline.match_input(in_df, "商品コード", None, jan_map, code_info)
    # 薄利（現価格1000）→ 80サイズの目標15%価格2148に引き上げ（常にmaxルール）
    rows = pipeline.build_price_rows(matched, "新下代", cost_table, P,
                                     cur_prices={"artc9999": 1000})
    assert rows[0]["新販売価格"] == 2148
    assert rows[0]["適用ルール"] == "目標利益率価格"
    # 手修正が最優先
    rows = pipeline.build_price_rows(matched, "新下代", cost_table, P,
                                     overrides={"artc9999": 3300}, cur_prices=RK_PRICES)
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
