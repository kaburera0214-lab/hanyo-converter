# -*- coding: utf-8 -*-
"""
価格改定の計算コア（Streamlit非依存の純粋関数）。

考え方（2026-07-14 ユーザー確定仕様）:
- 3980円（送料込みライン）未満の商品は、お客様が送料を別途支払う
  （宅配便は一律880円・メール便は一律350円）。したがって実際の入金は
  「販売価格＋加算分」＝**利益計算価格M**。3980円以上は送料無料なので M＝販売価格。
- 利益額 = M −（送料実費＋資材＋下代＋**M×手数料率**)×(1+税)
  …楽天の手数料は送料込みの決済総額Mに掛かる（2026-07-14ユーザー検算に合わせ修正）
  利益率 = 利益額 ÷ M   …旧・新とも同じ式で計算する（旧は旧下代・現販売価格）
- 目標利益率価格: 利益率がちょうど目標値（既定15%）に着地する販売価格を逆算する。
  M* = (送料+資材+新下代)×(1+税) ÷ ((1−目標) − 手数料率×(1+税)) → 販売価格に戻す
- 値上げ率価格: M(現販売価格)×(新下代÷旧下代) を送料込みベースで求め、
  実際の販売価格に戻す（3980円未満になるなら加算分を引く）。
  例) 現891円・宅配便: (891+880)×365/363=1781 → 1781−880=901円
- 下代値下げ→据え置き ／ 値上げ→「目標利益率価格」と「値上げ率価格」の高い方。

「直送価格＆送料変更」タブは送料込み換算なし（M=販売価格）・資材0・送料手入力で
同じ式を使う（mode="direct"）。
"""
from decimal import Decimal, ROUND_HALF_UP

# 計算パラメータ（サイドバーで一時変更可。恒久変更はここを修正）
DEFAULT_PARAMS = {
    "tax_rate": 0.10,           # 消費税率
    "fee_rate": 0.10,           # 楽天手数料率（送料込みの決済総額Mに対して）
    "target_margin": 0.15,      # 目標利益率（新価格はこの率にちょうど着地するよう逆算）
    "free_ship_line": 3980,     # この金額以上は送料無料（お客様の送料負担なし）
    "ship_included_line": 3300, # 本体価格(=送料を引いた価格)がこの値を超えたら送料込み・送料無料で
                                # 価格設定する（同時購入を狙える帯。2026-07-24ユーザー確定・可変）
    "takuhai_add": 880,         # 3980円未満・宅配便でお客様が払う送料
    "mail_add": 350,            # 3980円未満・メール便でお客様が払う送料
    "margin_warn": 0.10,        # 利益率がこの値未満なら警告（サイズ変更の利益チェックも共用）
}


def excel_round(x, digits=0):
    """ExcelのROUND（四捨五入・half-up）。Pythonのround（偶数丸め）とは違う。"""
    if x is None:
        return None
    q = Decimal(1).scaleb(-digits)
    v = Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)
    return int(v) if digits <= 0 else float(v)


def to_number(value, default=None):
    """"1,460" / " 930 " / 空欄 を数値に。数値にならなければ default。"""
    if value is None:
        return default
    s = str(value).strip().replace(",", "").replace("，", "")
    if s in ("", "-", "－", "nan", "None"):
        return default
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return default


def _add(delivery, params):
    """3980円未満でお客様が払う送料（配送種別ごとの一律額）。"""
    return params["mail_add"] if delivery == "メール便" else params["takuhai_add"]


def profit_base_price(price, delivery, params, mode="normal"):
    """利益計算価格M: 実際の入金額。3980円未満はお客様負担の送料を加算、以上は価格のまま。"""
    if mode == "direct" or price >= params["free_ship_line"]:
        return price
    return price + _add(delivery, params)


def m_to_price(m_value, delivery, params, mode="normal"):
    """利益計算価格M（送料込みベース）→ 実際に設定する販売価格。

    本体価格 p = M − 送料。pが「送料無料維持ライン」(ship_included_line, 既定3300)以下なら
    お客様が送料を負担する前提でpを設定（例 M=1781・宅配便 → 901円）。pがラインを超えたら
    送料込み・送料無料（価格 = M）で設定する（同時購入を狙える価格帯。例 M=4792 → 4792円）。
    """
    if mode == "direct":
        return int(m_value)
    p = m_value - _add(delivery, params)
    line = params.get("ship_included_line", params["free_ship_line"])
    return int(p) if p <= line else int(m_value)


def profit(m, cost, shipping, material, params):
    """利益額 = M −（送料実費＋資材＋下代＋M×手数料率)×(1+税)。
    手数料は送料込みの決済総額Mに掛かる。"""
    tax = 1 + params["tax_rate"]
    return m - (shipping + material + cost + m * params["fee_rate"]) * tax


def target_price(new_cost, shipping, material, delivery, params, mode="normal"):
    """
    利益率がちょうど目標値になる販売価格Pを逆算する。
      (1-t)・M = (送料+資材+新下代)×(1+税) + M×手数料率×(1+税)
    を送料込みベースMについて解き、実際の販売価格に戻す。
    解けない（手数料率が高すぎる等）場合は None。
    """
    t = params["target_margin"]
    denom = (1 - t) - params["fee_rate"] * (1 + params["tax_rate"])
    if denom <= 0:
        return None
    c = (shipping + material + new_cost) * (1 + params["tax_rate"])
    m_star = excel_round(c / denom)
    return max(m_to_price(m_star, delivery, params, mode), 0)


def compute_row(cur_price, new_cost, old_cost, shipping, material, delivery,
                params, mode="normal"):
    """
    1商品分の共通計算（新価格の決定はrules側）。
    返り値: dict(利益計算価格, 旧利益額, 旧利益率, 目標利益率価格)
    旧利益は「現販売価格・旧下代（不明なら新下代）」で計算する。
    """
    m_cur = profit_base_price(cur_price, delivery, params, mode)
    base_cost = old_cost if old_cost else new_cost
    old_profit = profit(m_cur, base_cost, shipping, material, params)
    return {
        "利益計算価格": m_cur,
        "旧利益額": old_profit,
        "旧利益率": (old_profit / m_cur) if m_cur else None,
        "目標利益率価格": target_price(new_cost, shipping, material, delivery, params, mode),
    }


def simulate_price(new_price, new_cost, shipping, material, delivery, params, mode="normal"):
    """新価格での(利益額, 利益率)。旧利益率と同じ式（M=送料込みベース）で計算する。"""
    if not new_price:
        return None, None
    m = profit_base_price(new_price, delivery, params, mode)
    p = profit(m, new_cost, shipping, material, params)
    return p, (p / m) if m else None


def output_prices(new_price, new_cost, params):
    """確定した新価格から3システム向けの値を作る。"""
    return {
        "NE売価": excel_round(new_price / (1 + params["tax_rate"])),
        "NE原価": new_cost,
        "楽天販売価格": new_price,
        "Yahoo販売価格": new_price,
    }


# 梱包サイズ変更の判定ロジックは pipeline.size_change_rows に集約
# （サイズアップ/ダウン分岐・便種変更・利益チェック・NG時の価格再設定）
