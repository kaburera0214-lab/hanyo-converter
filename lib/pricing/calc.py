# -*- coding: utf-8 -*-
"""
価格改定の計算コア（Streamlit非依存の純粋関数）。

考え方（2026-07-14 ユーザー確定仕様）:
- 3980円（送料込みライン）未満の商品は、お客様が送料を別途支払う
  （宅配便は一律880円・メール便は一律350円）。したがって実際の入金は
  「販売価格＋加算分」＝**利益計算価格M**。3980円以上は送料無料なので M＝販売価格。
- 利益額 = M −（送料実費＋資材＋下代)×(1+税) − 販売価格×手数料率×(1+税)
  利益率 = 利益額 ÷ M   …旧・新とも同じ式で計算する（旧は旧下代・現販売価格）
- 目標利益率価格: 利益率がちょうど目標値（既定15%）に着地する販売価格を逆算する。
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
    "fee_rate": 0.10,           # 楽天手数料率（販売価格に対して）
    "target_margin": 0.15,      # 目標利益率（新価格はこの率にちょうど着地するよう逆算）
    "free_ship_line": 3980,     # この金額以上は送料無料（お客様の送料負担なし）
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
    3980円未満に収まるなら送料加算分を引く。例) M=1781・宅配便 → 901円"""
    if mode == "direct":
        return int(m_value)
    p = m_value - _add(delivery, params)
    return int(p) if p < params["free_ship_line"] else int(m_value)


def profit(m, price, cost, shipping, material, params):
    """利益額 = M −（送料実費＋資材＋下代)×(1+税) − 価格×手数料率×(1+税)。"""
    tax = 1 + params["tax_rate"]
    return m - (shipping + material + cost) * tax - price * params["fee_rate"] * tax


def target_price(new_cost, shipping, material, delivery, params, mode="normal"):
    """
    利益率がちょうど目標値になる販売価格Pを逆算する。
      (1-t)・M(P) = (送料+資材+新下代)×(1+税) + P×手数料率×(1+税)
    を M(P)=P+加算（P<3980） / M(P)=P（P≥3980, direct） の両ケースで解き、
    整合する方を採用する。解けない（手数料率が高すぎる等）場合は None。
    """
    t = params["target_margin"]
    denom = (1 - t) - params["fee_rate"] * (1 + params["tax_rate"])
    if denom <= 0:
        return None
    c = (shipping + material + new_cost) * (1 + params["tax_rate"])
    if mode == "direct":
        return max(excel_round(c / denom), 0)
    p_under = (c - _add(delivery, params) * (1 - t)) / denom
    if p_under < params["free_ship_line"]:
        return max(excel_round(p_under), 0)
    return excel_round(c / denom)


def compute_row(cur_price, new_cost, old_cost, shipping, material, delivery,
                params, mode="normal"):
    """
    1商品分の共通計算（新価格の決定はrules側）。
    返り値: dict(利益計算価格, 旧利益額, 旧利益率, 目標利益率価格)
    旧利益は「現販売価格・旧下代（不明なら新下代）」で計算する。
    """
    m_cur = profit_base_price(cur_price, delivery, params, mode)
    base_cost = old_cost if old_cost else new_cost
    old_profit = profit(m_cur, cur_price, base_cost, shipping, material, params)
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
    p = profit(m, new_price, new_cost, shipping, material, params)
    return p, (p / m) if m else None


def output_prices(new_price, new_cost, params):
    """確定した新価格から3システム向けの値を作る。"""
    return {
        "NE売価": excel_round(new_price / (1 + params["tax_rate"])),
        "NE原価": new_cost,
        "楽天販売価格": new_price,
        "Yahoo販売価格": new_price,
    }


def size_change_check(cur_price, cost, shipping_new, material_new,
                      delivery_old, delivery_new, params):
    """
    梱包サイズ変更のチェック。
      利益チェック: 新サイズの送料・資材で（現価格のまま）利益率が margin_warn 以上か
      配送設定修正: 新旧で配送種別（宅配便/メール便）が変わるなら要修正
    返り値: dict(利益チェック, 配送設定要修正, 新利益額, 新利益率)
    """
    m = profit_base_price(cur_price, delivery_old, params)
    p = profit(m, cur_price, cost, shipping_new, material_new, params)
    margin = (p / m) if m else None
    profit_ok = "〇" if (margin is not None and margin >= params["margin_warn"]) else "×"
    return {
        "利益チェック": profit_ok,
        "配送設定要修正": "要修正" if delivery_old != delivery_new else "不要",
        "新利益額": p,
        "新利益率": margin,
    }
