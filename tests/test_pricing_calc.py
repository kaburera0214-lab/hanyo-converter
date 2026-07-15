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
    """ユーザー提示の実例: 現891円・宅配60・下代363→365 → 新価格901円"""
    base, price, rule = _decide(891, 365, 363, 675, 30.5, "宅配便")
    assert base["目標利益率価格"] == 580        # 15%にちょうど着地する価格
    assert price == 901 and rule == "値上げ率価格"
    profit, margin = calc.simulate_price(price, 365, 675, 30.5, "宅配便", P)
    assert round(profit) == 504 and abs(margin - 0.283) < 0.001
    assert calc.output_prices(price, 365, P)["NE売価"] == 819  # 901÷1.1


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
    """据え置き・下代同一なら旧利益率＝新利益率（同じ式・送料込みベース）"""
    base, price, rule = _decide(1914, 783, 783, 675, 30.5, "宅配便")
    assert price == 1914 and "据え置き" in rule
    _, new_margin = calc.simulate_price(price, 783, 675, 30.5, "宅配便", P)
    assert abs(base["旧利益率"] - new_margin) < 1e-9
    assert abs(new_margin - 0.3386) < 0.001    # はちまき1914円: M=2794で33.86%


def test_rule_price_down_keeps_current():
    """下代値下げ（新<旧）→据え置き"""
    _, price, rule = _decide(7150, 4000, 5200, 675, 30.5, "宅配便")
    assert price == 7150 and "据え置き" in rule


def test_rule_same_cost_ensures_target_margin():
    """下代が同額: 薄利なら目標利益率価格へ引き上げ、利益が足りていれば据え置き"""
    # 薄利（現440円・下代287のまま・60サイズ宅配 → 利益率-1.2%）→ 15%価格465円へ
    _, price, rule = _decide(440, 287, 287, 675, 30.5, "宅配便")
    assert price == 465 and rule == "目標利益率価格"
    _, margin = calc.simulate_price(price, 287, 675, 30.5, "宅配便", P)
    assert abs(margin - 0.15) < 0.01
    # 利益が足りている同額商品（33.9%）は据え置き（計算値が現価格以下）
    _, price2, rule2 = _decide(1914, 783, 783, 675, 30.5, "宅配便")
    assert price2 == 1914 and "据え置き" in rule2


def test_rule_floor_at_current_price():
    """計算値が現価格以下なら据え置き（値下げ事故防止。旧下代不明→目標価格のみの場合など）"""
    _, price, rule = _decide(9000, 5200, None, 675, 30.5, "宅配便")
    assert price == 9000 and "現価格以下" in rule  # 目標8778 < 現9000
    # 旧下代があれば値上げ率価格が現価格を必ず上回るのでそちらが採用される
    _, price2, rule2 = _decide(9000, 5200, 5100, 675, 30.5, "宅配便")
    assert price2 == 9176 and rule2 == "値上げ率価格"  # 9000×5200/5100


def test_direct_mode():
    """直送: 送料込み換算なし（M=価格）・送料手入力。miya0284直送: 送料1000→9216円"""
    base, price, rule = _decide(7150, 5200, 5100, 1000, 0, "宅配便", mode="direct")
    assert base["利益計算価格"] == 7150
    assert price == 9216
    _, margin = calc.simulate_price(price, 5200, 1000, 0, "宅配便", P, mode="direct")
    assert abs(margin - 0.15) < 0.01


def test_size_change_check():
    """サイズ変更チェック: メール便→宅配便は配送設定要修正、利益率で〇×"""
    chk = calc.size_change_check(
        cur_price=880, cost=446,
        shipping_new=269, material_new=23,
        delivery_old="メール便", delivery_new="メール便", params=P)
    assert chk["配送設定要修正"] == "不要"
    assert chk["利益チェック"] == "〇"
    chk2 = calc.size_change_check(
        cur_price=880, cost=446,
        shipping_new=675, material_new=30.5,
        delivery_old="メール便", delivery_new="宅配便", params=P)
    assert chk2["配送設定要修正"] == "要修正"
    assert chk2["利益チェック"] == "×"      # 送料が上がって利益率10%未満


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
