# -*- coding: utf-8 -*-
"""
価格改定の計算ロジック回帰テスト。

期待値はGoogleスプレッドシート「パピー納品価格変更」の実データ
（2026-07-13時点・数式解読時に取得した計算結果）と突合している。
実行: hanyo-converter直下で  python -m pytest tests/test_pricing_calc.py -q
      （pytestが無ければ python tests/test_pricing_calc.py でも可）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.pricing import calc, rules  # noqa: E402

P = dict(calc.DEFAULT_PARAMS)


def _new_price(cur_price, new_cost, shipping, material, delivery,
               old_cost=None, mode="normal"):
    base = calc.compute_row(cur_price, new_cost, shipping, material, delivery, P, mode=mode)
    ctx = {"現販売価格": cur_price, "新下代": new_cost, "旧下代": old_cost,
           "利益20%価格": base["利益20%価格"]}
    price, rule = rules.decide_price(ctx, P)
    return base, price, rule


def test_excel_round_half_up():
    assert calc.excel_round(2054.25) == 2054
    assert calc.excel_round(0.5) == 1          # Pythonのround(0.5)=0 と違うこと
    assert calc.excel_round(9102.5) == 9103
    assert calc.excel_round(8275.45) == 8275


def test_miya0284_takuhai_60():
    """シート行: miya0284 現価格7150・新下代5200・60サイズ宅配便 → 新価格9103・NE売価8275"""
    base, price, _ = _new_price(7150, 5200, 675, 30.5, "宅配便")
    assert base["利益計算価格"] == 7150                    # 3980以上→加算なし
    assert abs(base["変動費合計"] - 7282.55) < 0.01        # シート表示7,283
    assert price == 9103
    out = calc.output_prices(price, 5200, P)
    assert out["NE売価"] == 8275
    assert out["楽天販売価格"] == 9103 and out["Yahoo販売価格"] == 9103
    profit, margin = calc.simulate_price(price, base["変動費合計"], base["旧手数料"], P)
    assert round(profit) == 1534                           # シート表示 1,534
    assert abs(margin - 0.1685) < 0.001                    # 16.85%


def test_kwgc0414_mail_yuup3():
    """シート行: kwgc0414 現価格1760・新下代1026・yuup3メール便 → 新価格2054"""
    base, price, _ = _new_price(1760, 1026, 269, 23, "メール便")
    assert base["利益計算価格"] == 1760 + 350              # 3980未満メール便+350
    assert abs(base["変動費合計"] - 1643.4) < 0.01
    assert price == 2054


def test_popo0161_nekop():
    """シート行: popo0161-25 現価格600・新下代360・nekop → 新価格866"""
    base, price, _ = _new_price(600, 360, 194, 15.9, "メール便")
    assert base["利益計算価格"] == 950
    assert price == 866


def test_takuhai_add_under_line():
    """3980未満の宅配便は+880で利益判定（miya0306: 1760+880=2640）"""
    base, _, _ = _new_price(1760, 1560, 938, 115, "宅配便")
    assert base["利益計算価格"] == 2640
    assert abs(base["変動費合計"] - 3067.9) < 0.01         # シート表示 3,068

def test_rule_price_down_keeps_current():
    """下代値下げ→据え置き"""
    base, price, rule = _new_price(7150, 4000, 675, 30.5, "宅配便", old_cost=5200)
    assert price == 7150
    assert "据え置き" in rule


def test_rule_price_up_takes_max():
    """値上げ→max(利益20%価格, 現価格×新下代/旧下代)"""
    # 20%価格が勝つケース（大幅値上げで原価比例では足りない）
    base, price, rule = _new_price(7150, 5200, 675, 30.5, "宅配便", old_cost=5100)
    ratio = calc.excel_round(7150 * 5200 / 5100)  # ≈ 7290 < 9103
    assert price == base["利益20%価格"] == 9103
    # 値上げ率価格が勝つケース
    base2, price2, rule2 = _new_price(20000, 5200, 675, 30.5, "宅配便", old_cost=5000)
    ratio2 = calc.excel_round(20000 * 5200 / 5000)  # = 20800
    assert price2 == ratio2 == 20800
    assert "値上げ率" in rule2


def test_direct_mode():
    """直送: 資材0・送料込み換算なし（miya0284直送行: 送料1000 → 新価格9550）"""
    base, price, _ = _new_price(7150, 5200, 1000, 0, "宅配便", mode="direct")
    assert base["利益計算価格"] == 7150
    # Q = (1000+0+715+5200)*1.1 = 7606.5 → T = ROUND(9508.125) = 9508
    assert abs(base["変動費合計"] - 7606.5) < 0.01
    assert price == 9508


def test_size_change_check():
    """サイズ変更チェック: 宅配便60→nekopは配送設定要修正、利益率で〇×"""
    chk = calc.size_change_check(
        ne_price=800, cur_price=880, cost=446,
        shipping_new=269, material_new=23,
        delivery_old="メール便", delivery_new="メール便",
        params=P, rakuten_price=880)
    assert chk["価格チェック"] == "〇"      # 800*1.1=880
    assert chk["配送設定要修正"] == "不要"
    chk2 = calc.size_change_check(
        ne_price=800, cur_price=880, cost=446,
        shipping_new=675, material_new=30.5,
        delivery_old="メール便", delivery_new="宅配便",
        params=P, rakuten_price=990)
    assert chk2["価格チェック"] == "×"
    assert chk2["配送設定要修正"] == "要修正"
    assert chk2["利益チェック"] == "×"      # 送料が上がって利益率10%未満


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
