# -*- coding: utf-8 -*-
"""
新販売価格の決定ルール（拡張ポイント）。

運用ルール（ユーザー確定）:
  ■下代が値下げ → 価格据え置き
  ■下代が値上げ → 「目標利益率価格」と「値上げ率価格」を比較して高い方を設定
    - 目標利益率価格: 利益率がちょうど目標値（既定15%）に着地する価格（calc.target_price）
    - 値上げ率価格: 送料込みベースM(現販売価格)×(新下代÷旧下代) を実際の販売価格に戻したもの
      例) 現891円・宅配便・下代363→365: (891+880)×365/363=1781 → 1781−880=901円
    - 計算値が現販売価格を下回る場合は据え置き（値上げ判定で値下げしてしまう事故防止）

各ルールは ctx を受けて (新価格, ルール名) か None（次のルールへ）を返す関数。
DEFAULT_RULES の先頭から順に評価し、最初に価格を返したルールで確定する。
商品個別ルールや%指定を足すときは、関数を書いて DEFAULT_RULES の適切な位置に挿すだけでよい。

ctx のキー:
  現販売価格 / 利益計算価格(=M(現販売価格)) / 新下代 / 旧下代 / 目標利益率価格 /
  配送種別 / mode / 指定価格(任意) / 値上げ率(任意・%)
"""
from . import calc


def _ratio_price(ctx, params, factor):
    """送料込みベースMに倍率を掛けて、実際の販売価格に戻す。"""
    m = calc.excel_round(ctx["利益計算価格"] * factor)
    return calc.m_to_price(m, ctx.get("配送種別", ""), params, ctx.get("mode", "normal"))


def rule_fixed_price(ctx, params):
    """入力CSVに「指定価格」列があればそれを最優先（商品個別ルールの受け皿）。"""
    p = calc.to_number(ctx.get("指定価格"))
    if p:
        return int(p), "指定価格"
    return None


def rule_markup_percent(ctx, params):
    """入力CSVに「値上げ率」列（%）があれば送料込みベース×(1+率)で決める。"""
    r = calc.to_number(ctx.get("値上げ率"))
    if r:
        return _ratio_price(ctx, params, 1 + r / 100.0), f"値上げ率{r}%"
    return None


def rule_price_down_keep(ctx, params):
    """下代が値下げ（または同額）なら販売価格は据え置き。"""
    old = calc.to_number(ctx.get("旧下代"))
    new = calc.to_number(ctx.get("新下代"))
    if old and new is not None and new <= old:
        return int(ctx["現販売価格"]), "据え置き(下代値下げ)"
    return None


def rule_price_up_max(ctx, params):
    """下代値上げ: max(目標利益率価格, 値上げ率価格)。ただし現販売価格は下回らない。
    旧下代が不明なら目標利益率価格のみで決める。"""
    t_price = ctx["目標利益率価格"]
    old = calc.to_number(ctx.get("旧下代"))
    new = calc.to_number(ctx.get("新下代"))
    cur = int(ctx["現販売価格"])

    candidates = []
    if t_price:
        candidates.append((t_price, "目標利益率価格"))
    if old and new:
        candidates.append((_ratio_price(ctx, params, new / old), "値上げ率価格"))
    if not candidates:
        return cur, "据え置き(目標価格を計算できず)"

    price, name = max(candidates, key=lambda c: c[0])
    if price <= cur:
        return cur, "据え置き(計算値が現価格以下)"
    if not old:
        name += "(旧下代不明)"
    return price, name


DEFAULT_RULES = [rule_fixed_price, rule_markup_percent, rule_price_down_keep, rule_price_up_max]


def decide_price(ctx, params, rules=None):
    """ルールを順に適用して (新価格, 適用ルール名) を返す。"""
    for rule in (rules or DEFAULT_RULES):
        result = rule(ctx, params)
        if result is not None:
            return result
    return int(ctx["現販売価格"]), "据え置き(ルール該当なし)"
