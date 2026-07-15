# -*- coding: utf-8 -*-
"""
新販売価格の決定ルール（拡張ポイント）。

運用ルール（2026-07-14 ユーザー確定・シンプル設計）:
  アップしたCSVの商品はすべて「目標利益率価格」と「値上げ率価格」の高い方に設定する。
  据え置きルールは無し（下代が値下げなら価格も下がり得る。目標利益率は必ず確保される）。
    - 目標利益率価格: 利益率がちょうど目標値（既定15%）に着地する価格（calc.target_price）
    - 値上げ率価格: 送料込みベースM(現販売価格)×(新下代÷旧下代) を実際の販売価格に戻したもの
      例) 現891円・宅配便・下代363→365: (891+880)×365/363=1781 → 1781−880=901円

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


def rule_max_price(ctx, params):
    """max(目標利益率価格, 値上げ率価格)。旧下代が不明なら目標利益率価格のみで決める。"""
    t_price = ctx["目標利益率価格"]
    old = calc.to_number(ctx.get("旧下代"))
    new = calc.to_number(ctx.get("新下代"))

    candidates = []
    if t_price:
        candidates.append((t_price, "目標利益率価格"))
    if old and new:
        candidates.append((_ratio_price(ctx, params, new / old), "値上げ率価格"))
    if not candidates:
        return int(ctx["現販売価格"]), "据え置き(目標価格を計算できず)"

    price, name = max(candidates, key=lambda c: c[0])
    if not old:
        name += "(旧下代不明)"
    return price, name


DEFAULT_RULES = [rule_fixed_price, rule_markup_percent, rule_max_price]


def decide_price(ctx, params, rules=None):
    """ルールを順に適用して (新価格, 適用ルール名) を返す。"""
    for rule in (rules or DEFAULT_RULES):
        result = rule(ctx, params)
        if result is not None:
            return result
    return int(ctx["現販売価格"]), "据え置き(ルール該当なし)"
