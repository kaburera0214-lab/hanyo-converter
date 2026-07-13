# -*- coding: utf-8 -*-
"""
新販売価格の決定ルール（拡張ポイント）。

シート冒頭メモの運用ルールを実装:
  ■下代が値下げ → 価格据え置き
  ■下代が値上げ → 「利益20%確保価格」と「値上げ率価格（現価格×新下代÷旧下代）」の高い方

各ルールは ctx を受けて (新価格, ルール名) か None（次のルールへ）を返す関数。
DEFAULT_RULES の先頭から順に評価し、最初に価格を返したルールで確定する。
商品個別ルールや%指定を足すときは、関数を書いて DEFAULT_RULES の適切な位置に挿すだけでよい。

ctx のキー:
  現販売価格 / 新下代 / 旧下代 / 利益20%価格 / 指定価格(任意) / 値上げ率(任意・%)
"""
from . import calc


def rule_fixed_price(ctx, params):
    """入力CSVに「指定価格」列があればそれを最優先（商品個別ルールの受け皿）。"""
    p = calc.to_number(ctx.get("指定価格"))
    if p:
        return int(p), "指定価格"
    return None


def rule_markup_percent(ctx, params):
    """入力CSVに「値上げ率」列（%）があれば 現価格×(1+率) で決める。"""
    r = calc.to_number(ctx.get("値上げ率"))
    if r:
        return calc.excel_round(ctx["現販売価格"] * (1 + r / 100.0)), f"値上げ率{r}%"
    return None


def rule_price_down_keep(ctx, params):
    """下代が値下げ（または同額）なら販売価格は据え置き。"""
    old = calc.to_number(ctx.get("旧下代"))
    new = calc.to_number(ctx.get("新下代"))
    if old and new is not None and new <= old:
        return int(ctx["現販売価格"]), "据え置き(下代値下げ)"
    return None


def rule_price_up_max(ctx, params):
    """下代値上げ: max(利益20%確保価格, 値上げ率価格=現価格×新下代÷旧下代)。
    旧下代が不明なら利益20%確保価格のみで決める。"""
    t20 = ctx["利益20%価格"]
    old = calc.to_number(ctx.get("旧下代"))
    new = calc.to_number(ctx.get("新下代"))
    if old and new:
        ratio_price = calc.excel_round(ctx["現販売価格"] * new / old)
        if ratio_price > t20:
            return ratio_price, "値上げ率価格(現価格×新下代/旧下代)"
    if old:
        return t20, "利益20%確保価格"
    return t20, "利益20%確保価格(旧下代不明)"


DEFAULT_RULES = [rule_fixed_price, rule_markup_percent, rule_price_down_keep, rule_price_up_max]


def decide_price(ctx, params, rules=None):
    """ルールを順に適用して (新価格, 適用ルール名) を返す。"""
    for rule in (rules or DEFAULT_RULES):
        result = rule(ctx, params)
        if result is not None:
            return result
    return int(ctx["現販売価格"]), "据え置き(ルール該当なし)"
