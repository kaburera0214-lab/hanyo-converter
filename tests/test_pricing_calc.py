# -*- coding: utf-8 -*-
"""
価格改定の計算ロジックテスト。

期待値は2026-07-14にユーザーが提示した実例（artc0486）と確定仕様に基づく:
- 3980円未満はお客様が送料を払う（宅配880/メール350）→ 利益は送料込みベースMで判定
- 目標利益率価格 = 利益率がちょうど目標値(15%)に着地する価格を逆算
- 値上げ率価格 = M(現価格)×(新下代/旧下代) を販売価格に戻す
  例) (891+880)×365/363=1781 → 1781−880=901円
実行: hanyo-converter直下で  python tests/test_pricing_calc.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.pricing import calc, rules  # noqa: E402

P = dict(calc.DEFAULT_PARAMS)  # 目標利益率15%


def _decide(cur_price, new_cost, old_cost, shipping, material, delivery, mode="normal"):
    base = calc.compute_row(cur_price, new_cost, old_cost, shipping, material,
                            delivery, P, mode=mode)
    ctx = {"現販売価格": cur_price, "利益計算価格": base["利益計算価格"],
           "新下代": new_cost, "旧下代": old_cost,
           "目標利益率価格": base["目標利益率価格"],
           "配送種別": delivery, "mode": mode}
    price, rule = rules.decide_price(ctx, P)
    return base, price, rule


def test_excel_round_half_up():
    assert calc.excel_round(0.5) == 1          # Pythonのround(0.5)=0 と違うこと
    assert calc.excel_round(1780.76) == 1781
    assert calc.excel_round(580.47) == 580


def test_profit_base_and_m_to_price():
    """利益計算価格M（送料込みベース）と、Mから販売価格への逆変換"""
    assert calc.profit_base_price(891, "宅配便", P) == 1771     # +880
    assert calc.profit_base_price(600, "メール便", P) == 950    # +350
    assert calc.profit_base_price(7150, "宅配便", P) == 7150    # 3980以上は加算なし
    assert calc.m_to_price(1781, "宅配便", P) == 901            # ユーザー実例
    assert calc.m_to_price(2405, "メール便", P) == 2055
    assert calc.m_to_price(7290, "宅配便", P) == 7290           # 引くと3980以上→そのまま
    assert calc.m_to_price(5000, "宅配便", P, mode="direct") == 5000


def test_artc0486_user_example():
    """実例: 現891円・宅配60・下代363→365。
    値上げ率は送料を含まない現販売価格に掛ける（2026-07-16確定）→ 891×365/363=896円"""
    base, price, rule = _decide(891, 365, 363, 675, 30.5, "宅配便")
    assert base["目標利益率価格"] == 711        # 15%にちょうど着地する価格（M*=1591−880）
    assert price == 896 and rule == "値上げ率価格"
    profit, margin = calc.simulate_price(price, 365, 675, 30.5, "宅配便", P)
    assert round(profit) == 403 and abs(margin - 0.2270) < 0.001
    assert calc.output_prices(price, 365, P)["NE売価"] == 815  # 896÷1.1


def test_markup_percent_excludes_shipping():
    """値上げ率%は送料を含まない価格に掛ける（ユーザー実例: 1100円×30%→1430円。
    送料込みベースに掛けた1694円は誤り）"""
    ctx = {"現販売価格": 1100, "利益計算価格": 1980, "値上げ率": "30",
           "配送種別": "宅配便", "mode": "normal"}
    price, name = rules.rule_markup_percent(ctx, P)
    assert price == 1430 and name == "値上げ率30%"


def test_fee_on_total_payment():
    """手数料は送料込みの決済総額Mに掛かる（ユーザー手計算 2026-07-14 と一致すること）:
    販売価格(込)2794・原価783・送料675・資材75.83
    → 総コスト=(783+279.4+675+75.83)×1.1=1994.553 → 利益799.447"""
    p = calc.profit(2794, 783, 675, 75.83, P)
    assert abs(p - 799.447) < 0.01


def test_target_price_lands_on_target_margin():
    """目標利益率価格は実際にその利益率に着地する（丸め誤差±1%以内）"""
    cases = [
        (365, 675, 30.5, "宅配便"),    # 3980未満・宅配
        (1026, 269, 23, "メール便"),   # 3980未満・メール
        (5200, 675, 30.5, "宅配便"),   # 3980以上
    ]
    for cost, ship, mat, deliv in cases:
        tp = calc.target_price(cost, ship, mat, deliv, P)
        _, margin = calc.simulate_price(tp, cost, ship, mat, deliv, P)
        assert abs(margin - P["target_margin"]) < 0.01, (cost, tp, margin)
    # 3980以上のケースの具体値（miya0284: 下代5200・60サイズ → 8778円）
    assert calc.target_price(5200, 675, 30.5, "宅配便", P) == 8778


def test_old_and_new_margin_same_formula():
    """価格・下代とも変わらなければ旧利益率＝新利益率（同じ式・送料込みベース）"""
    base, price, rule = _decide(1914, 783, 783, 675, 30.5, "宅配便")
    assert price == 1914
    _, new_margin = calc.simulate_price(price, 783, 675, 30.5, "宅配便", P)
    assert abs(base["旧利益率"] - new_margin) < 1e-9
    assert abs(new_margin - 0.3040) < 0.001    # はちまき1914円: M=2794で30.4%


def test_always_max_rule():
    """常に max(目標利益率価格, 値上げ率価格)。据え置きルールは無し"""
    # 値下げ（5200→4000）: 目標15%価格6995 > 値上げ率価格5500 → 6995（価格も下がる）
    _, price, rule = _decide(7150, 4000, 5200, 675, 30.5, "宅配便")
    assert price == 6995 and rule == "目標利益率価格"
    # 同額・薄利（現440円・下代287のまま）→ 15%価格595円へ
    _, price2, rule2 = _decide(440, 287, 287, 675, 30.5, "宅配便")
    assert price2 == 595 and rule2 == "目標利益率価格"
    _, margin = calc.simulate_price(price2, 287, 675, 30.5, "宅配便", P)
    assert abs(margin - 0.15) < 0.01
    # 同額・利益が足りている商品は値上げ率価格=現価格が高い方 → 変わらず
    _, price3, rule3 = _decide(1914, 783, 783, 675, 30.5, "宅配便")
    assert price3 == 1914 and rule3 == "値上げ率価格"


def test_rule_without_old_cost():
    """旧下代不明なら目標利益率価格のみで決める"""
    _, price, rule = _decide(9000, 5200, None, 675, 30.5, "宅配便")
    assert price == 8778 and rule == "目標利益率価格(旧下代不明)"


def test_direct_mode():
    """直送: 送料込み換算なし（M=価格）・送料手入力。miya0284直送: 送料1000→9216円"""
    base, price, rule = _decide(7150, 5200, 5100, 1000, 0, "宅配便", mode="direct")
    assert base["利益計算価格"] == 7150
    assert price == 9216
    _, margin = calc.simulate_price(price, 5200, 1000, 0, "宅配便", P, mode="direct")
    assert abs(margin - 0.15) < 0.01


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
