# -*- coding: utf-8 -*-
"""
入荷登録（lib/receiving・lib/ne_api）の純関数テスト。
実行: hanyo-converter直下で  python tests/test_receiving.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.ne_api import goods  # noqa: E402
from lib.pricing import calc, masters  # noqa: E402
from lib.receiving import plan as rp  # noqa: E402

P = dict(calc.DEFAULT_PARAMS)


def _ne_master():
    # newi0001 は項目1未設定（初回登録のケース）
    csv = (
        "商品コード,商品名,JANコード,原価,項目1\n"
        "slvb0144,テストサイズ変更,4900000000144,480,yuup3\n"
        "kwgc0414,テストKWGC,4900000000414,900,yuup3\n"
        "artc9999,テスト値下げ,4900000009999,1500,80\n"
        "newi0001,テスト新規入荷,4900000000001,300,\n"
    ).encode("cp932")
    df, _ = masters.load_ne_master(csv)
    return masters.build_lookup(df)


def _cost_table():
    return masters.cost_lookup(masters.load_cost_master_bundled())


RK_PRICES = {"slvb0144": 500, "kwgc0414": 1760, "artc9999": 2200}


def test_location_code_and_split():
    assert rp.location_code("100A", "TA10B") == "100A-TA10B"
    assert rp.location_code(" 60B ", " KA01 ") == "60B-KA01"
    mats, locs = rp.split_location_values(
        ["100A-TA10B", "100A-TA11C", "60B-KA01", "", "nan", "半端な値", None,
         "100A-TA10B"])  # 重複・不正値は無視
    assert mats == ["100A", "60B"] or mats == sorted(["100A", "60B"])
    assert set(mats) == {"100A", "60B"}
    assert set(locs) == {"TA10B", "TA11C", "KA01"}
    # ロケーション側にハイフンがあっても最初の「-」でだけ分割する
    mats2, locs2 = rp.split_location_values(["100A-TA-10B"])
    assert mats2 == ["100A"] and locs2 == ["TA-10B"]
    # 件数集計（誤登録の点検用・split_location_valuesと同じ分割規則）
    mc, lc = rp.split_location_counts(
        ["100A-TA10B", "100A-TA11C", "60B-KA01", "100A-TA10B", "", "半端な値"])
    assert mc == {"100A": 3, "60B": 1}
    assert lc == {"TA10B": 2, "TA11C": 1, "KA01": 1}


def test_build_plan():
    _, code_info = _ne_master()
    cost_table = _cost_table()
    rows = [
        # サイズアップ＋便種変更＋利益NG（現価格500円 → 882円に再設定）
        {"商品コード": "slvb0144", "資材ナンバー": "60A", "ロケーション": "TA10B", "配送サイズ": "60"},
        # サイズアップ＋便種変更・利益OK（価格そのまま）
        {"商品コード": "kwgc0414", "資材ナンバー": "60A", "ロケーション": "TA11C", "配送サイズ": "60"},
        # 変更なし（項目1同値 → NEには同値を送る・モール修正なし）
        {"商品コード": "artc9999", "資材ナンバー": "80B", "ロケーション": "KA01", "配送サイズ": "80"},
        # 初回登録（項目1が空 → 設定するだけ）
        {"商品コード": "newi0001", "資材ナンバー": "100A", "ロケーション": "TB05", "配送サイズ": "100"},
    ]
    out = rp.build_plan(rows, code_info, cost_table, P, cur_prices=RK_PRICES)
    by_code = {r["商品コード"]: r for r in out}

    r = by_code["slvb0144"]
    assert r["ロケーションコード"] == "60A-TA10B"
    assert r["区分"] == "サイズアップ"
    assert r["配送設定"] == "要修正（メール便→宅配便）"
    assert r["利益チェック"] == "×"
    assert r["新販売価格"] == 882 and r["NE売価"] == 802

    r = by_code["kwgc0414"]
    assert r["区分"] == "サイズアップ" and r["利益チェック"] == "〇"
    assert r["新販売価格"] is None

    r = by_code["artc9999"]
    assert r["区分"] == "変更なし" and r["配送設定"] == "不要"
    assert r["新項目1"] == "80" and r["新販売価格"] is None

    r = by_code["newi0001"]
    assert r["区分"] == "初回登録" and r["配送設定"] == "不要"
    assert r["ロケーションコード"] == "100A-TB05" and r["新項目1"] == "100"

    # NE更新バッチ: ①は全行（空値なし）②は価格再設定行のみ
    main_rows, price_rows = rp.ne_rows_from_plan(out)
    assert len(main_rows) == 4
    assert {"syohin_code": "slvb0144", "location": "60A-TA10B", "org1": "60"} in main_rows
    assert {"syohin_code": "artc9999", "location": "80B-KA01", "org1": "80"} in main_rows
    assert price_rows == [{"syohin_code": "slvb0144", "baika_tnk": 802}]

    # 便種変更の親まとめ（対応表なし＝枝番なしコードはそのまま親）
    dv = rp.delivery_rows(out, {})
    assert [d["商品管理番号"] for d in dv] == ["slvb0144", "kwgc0414"]
    assert dv[0]["新便種"] == "宅配便"

    # 価格PATCHタスク（単品SKU扱い）
    tasks, missing = rp.price_tasks(out, code_info, {})
    assert missing == []
    assert tasks == [{"商品管理番号": "slvb0144", "sku_prices": {"slvb0144": 882},
                      "対象コード": ["slvb0144"]}]

    # SKU対応表がある場合はその親/SKU番号を使う
    sku_table = {"slvb0144": ("slvb0100", "8577", "slvb0144")}
    tasks2, _ = rp.price_tasks(out, code_info, sku_table)
    assert tasks2[0]["商品管理番号"] == "slvb0100"
    assert tasks2[0]["sku_prices"] == {"8577": 882}

    # 枝番付きで対応表に無いコードは missing（楽天PATCH不可）
    plan_edaban = [dict(out[0], **{"商品コード": "slvb0144-01"})]
    _, missing2 = rp.price_tasks(plan_edaban, code_info, {})
    assert missing2 == ["slvb0144-01"]

    # 証跡CSV一式
    files = rp.evidence_files(out, dv, code_info, {})
    assert set(files) == {"ne_location_update.csv", "rakuten_delivery_update.csv",
                          "yahoo_delivery_update.csv", "normal-item.csv",
                          "yahoo_data.csv", "ne_price_update.csv", "receiving_detail.csv"}
    loc_csv = files["ne_location_update.csv"].decode("cp932")
    assert loc_csv.splitlines()[0] == "syohin_code,location,org1"
    assert "newi0001,100A-TB05,100" in loc_csv
    assert "slvb0144,802,480" in files["ne_price_update.csv"].decode("cp932")  # NE売価=税抜


def test_default_size():
    opts = ["nekop", "60", "80", "100", "120", "140", "160"]
    assert rp.default_size("60A", opts) == "60"
    assert rp.default_size("80B", opts) == "80"
    assert rp.default_size("100A", opts) == "100"
    assert rp.default_size("160B", opts) == "160"
    assert rp.default_size("MB2", opts) == "nekop"
    assert rp.default_size("MB5", opts) == "nekop"
    assert rp.default_size("ND", opts) is None      # セットなし
    assert rp.default_size("ST", opts) is None      # セットなし
    assert rp.default_size("", opts) is None
    assert rp.default_size(None, opts) is None
    assert rp.default_size("999X", opts) is None     # size_optsに無い数字は安全側でNone


def test_ne_build_csv_rejects_bad_rows():
    csv_text = goods.build_csv([{"syohin_code": "abc0001", "location": "100A-TA10B",
                                 "org1": "80"}])
    assert csv_text.splitlines()[0] == "syohin_code,location,org1"
    assert "abc0001,100A-TA10B,80" in csv_text
    # 空値は送らない設計（NE側の挙動が未定義のため）
    try:
        goods.build_csv([{"syohin_code": "abc0001", "location": ""}])
        raise AssertionError("空値を検出できていない")
    except ValueError:
        pass
    # 行ごとに列が違うのも不可（列を混在させない）
    try:
        goods.build_csv([{"syohin_code": "a", "org1": "60"}, {"syohin_code": "b"}])
        raise AssertionError("列の混在を検出できていない")
    except ValueError:
        pass
    # syohin_code必須
    try:
        goods.build_csv([{"location": "100A-TA10B"}])
        raise AssertionError("syohin_code欠落を検出できていない")
    except ValueError:
        pass


def test_ne_usage_level():
    from lib.ne_api import usage
    assert usage._level(0, 1000, 0.8) == "ok"
    assert usage._level(799, 1000, 0.8) == "ok"
    assert usage._level(800, 1000, 0.8) == "warn"    # 80%到達で警告
    assert usage._level(999, 1000, 0.8) == "warn"
    assert usage._level(1000, 1000, 0.8) == "over"   # 上限到達で課金
    assert usage._level(1500, 1000, 0.8) == "over"
    assert usage._level(500, 0, 0.8) == "ok"          # 上限0（無効）は常にok


def test_master_name_parse_and_latest():
    """手動(master_)とAPI自動(master_auto_)の新旧判定は末尾の日付+版で行う（名前降順は誤り）"""
    from lib import master_store as ms
    assert ms._parse_master_name("master_20260724_003.csv") == ("20260724", 3, "手動アップ")
    assert ms._parse_master_name("master_auto_20260725_001.csv") == ("20260725", 1, "自動(API)")
    assert ms._parse_master_name("master.csv") is None
    # 版キー(date, ver)で新しい方が勝つ: auto_0725 > manual_0724（名前昇順だと逆転する）
    a = ms._parse_master_name("master_20260724_009.csv")     # 手動・古い日付
    b = ms._parse_master_name("master_auto_20260725_001.csv")  # 自動・新しい日付
    assert (b[0], b[1]) > (a[0], a[1])
    # 同日は版で比較
    c = ms._parse_master_name("master_20260724_010.csv")
    assert (c[0], c[1]) > (a[0], a[1])


def test_price_patch_body():
    from lib.pricing import rakuten_price
    body = rakuten_price.price_patch_body({"8577": 882, "kei0018": 2783})
    # 表示価格(referencePrice)=販売価格・当店通常価格(type1)も一緒に更新する
    assert body == {"variants": {
        "8577": {"standardPrice": "882",
                 "referencePrice": {"displayType": "REFERENCE_PRICE", "type": 1,
                                    "value": "882"}},
        "kei0018": {"standardPrice": "2783",
                    "referencePrice": {"displayType": "REFERENCE_PRICE", "type": 1,
                                       "value": "2783"}}}}


def test_split_by_existence():
    # NEに存在する行はNEの正確な商品コードへ置換、存在しない行はmissingに
    rows = [{"syohin_code": "kawa3935", "location": "60A-TB1C", "org1": "nekop"},
            {"syohin_code": "KAWA9999", "location": "80A-TA1A", "org1": "80"}]
    found = {"kawa3935": "KAWA3935"}          # NEは大文字で保持していた例
    ok, missing = rp.split_by_existence(rows, found)
    assert ok == [{"syohin_code": "KAWA3935", "location": "60A-TB1C", "org1": "nekop"}]
    assert missing == ["KAWA9999"]
    # 全部見つかる/全部見つからない
    ok2, miss2 = rp.split_by_existence(rows, {"kawa3935": "kawa3935",
                                              "kawa9999": "kawa9999"})
    assert len(ok2) == 2 and miss2 == []
    ok3, miss3 = rp.split_by_existence(rows, {})
    assert ok3 == [] and miss3 == ["kawa3935", "KAWA9999"]


def test_receiving_master_norm_list():
    from lib.receiving import master as rm
    # 正規化・重複除去・空値除去、登録順は保持
    assert rm._norm_list(["100A", "60B", "100A", "", "nan", None, " 60A "]) == \
        ["100A", "60B", "60A"]
    # 初期資材ナンバー（ユーザー確定19種）で重複なし
    assert len(rm.DEFAULT_MATERIALS) == 19
    assert len(set(rm.DEFAULT_MATERIALS)) == 19
    assert "MB2" in rm.DEFAULT_MATERIALS and "ST" in rm.DEFAULT_MATERIALS


def test_receiving_master_locations():
    from lib.receiving import master as rm
    rows = rm.norm_locations([
        ("トイプー", "TA", "TA10B"),
        ("梱包室", "CB1", ""),            # 第三階層なし → 第二階層が最下層
        ("トイプー", "TA", "TA10B"),      # 最下層コードの重複は除去
        ("", "", ""),                     # 第二階層が無い行は捨てる
        {"l1": "事務所", "l2": "JT", "l3": ""},
        "PA1",                            # 旧形式（フラットな文字列）からの移行
    ])
    assert rows == [("トイプー", "TA", "TA10B"), ("梱包室", "CB1", ""),
                    ("事務所", "JT", ""), ("", "PA1", "")]
    # NEに書くのは最下層の値
    assert rm.location_code(("トイプー", "TA", "TA10B")) == "TA10B"
    assert rm.location_code(("梱包室", "CB1", "")) == "CB1"
    # 階層選択用の入れ子（第三階層が無い棚は空リスト）
    tree = rm.hierarchy(rows)
    assert tree["トイプー"]["TA"] == ["TA10B"]
    assert tree["梱包室"]["CB1"] == []
    # まとめて入力用のフラット選択肢（ラベルは階層をつなげたもの）
    flat = rm.flat_options(rows)
    assert flat[0] == ("トイプー ｜ TA ｜ TA10B", "TA10B")
    assert flat[1] == ("梱包室 ｜ CB1", "CB1")


def test_bundled_locations():
    """同梱のロケ一覧（ロケ一覧.xlsx由来）が読め、最下層コードが一意であること"""
    from lib.receiving import master as rm
    rows = rm.load_bundled_locations()
    assert len(rows) == 492
    codes = [rm.location_code(r) for r in rows]
    assert len(set(codes)) == len(codes)      # 最下層コードは全件ユニーク
    assert ("トイプー", "TA", "TA10B") in rows  # NE実データ 100A-TA10B と整合
    assert ("梱包室", "CB1", "") in rows        # 2階層までの棚
    tree = rm.hierarchy(rows)
    assert set(tree) == {"トイプー", "シュナ", "ポメ", "梱包室", "事務所", "TeamEC"}


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except AssertionError:
                fails += 1
                import traceback
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)
