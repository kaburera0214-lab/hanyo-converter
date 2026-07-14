# -*- coding: utf-8 -*-
"""
価格改定の計算コア（Streamlit非依存の純粋関数）。

Googleスプレッドシート「パピー納品価格変更」の数式を再現する:
  利益計算価格 M = 現販売価格（3980円以上）／未満なら送料込み換算（宅配便+880・メール便+350）
  変動費合計 Q = (送料 + 資材 + 楽天手数料 + 新下代) × 1.1
  利益20%確保価格 T = ROUND(Q ÷ 0.8)   ※ExcelのROUND＝四捨五入
  新価格での利益 U = T − (Q − 旧手数料 + T×0.1×1.1)
  NE売価 = ROUND(T ÷ 1.1)（税抜） / NE単価(原価) = 新下代 / 楽天・Yahoo = T（税込）

「直送価格＆送料変更」タブは 資材0・送料手入力・送料込み換算なし（M=現販売価格）で
同じ式を使う（mode="direct"）。
"""
from decimal import Decimal, ROUND_HALF_UP

# 計算パラメータ（サイドバーで一時変更可。恒久変更はここを修正）
DEFAULT_PARAMS = {
    "tax_rate": 0.10,           # 消費税率
    "fee_rate": 0.10,           # 楽天手数料率（現販売価格に対して）
    "target_cost_ratio": 0.80,  # 新価格 = 変動費 ÷ この値（0.8 = 利益率20%確保）
    "free_ship_line": 3980,     # この金額以上は送料込み扱い（加算なし）
    "takuhai_add": 880,         # 3980円未満・宅配便の送料込み換算加算
    "mail_add": 350,            # 3980円未満・メール便の送料込み換算加算
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


def profit_base_price(price, delivery, params):
    """利益計算価格（M列）: 3980円未満は送料込みに換算して利益を判定する。"""
    line = params["free_ship_line"]
    if price >= line:
        return price
    if delivery == "メール便":
        return price + params["mail_add"]
    return price + params["takuhai_add"]


def variable_cost(price, new_cost, shipping, material, params):
    """(旧手数料, 変動費合計Q) を返す。Q = (送料+資材+手数料+新下代)×(1+税率)。"""
    fee = price * params["fee_rate"]
    q = (shipping + material + fee + new_cost) * (1 + params["tax_rate"])
    return fee, q


def simulate_price(new_price, q, fee_old, params):
    """新価格での(利益額U, 利益率V)。Qのうち手数料だけ新価格ベース（税込）に差し替える。"""
    if not new_price:
        return None, None
    u = new_price - (q - fee_old + new_price * params["fee_rate"] * (1 + params["tax_rate"]))
    return u, u / new_price


def compute_row(cur_price, new_cost, shipping, material, delivery, params, mode="normal"):
    """
    1商品分の共通計算（新価格の決定はrules側）。
    mode: "normal"=納品価格変更 / "direct"=直送（利益計算価格=現販売価格のまま）
    返り値: dict(利益計算価格, 旧手数料, 変動費合計, 旧利益額, 旧利益率, 利益20%価格)
    """
    fee_old, q = variable_cost(cur_price, new_cost, shipping, material, params)
    if mode == "direct":
        m = cur_price
    else:
        m = profit_base_price(cur_price, delivery, params)
    old_profit = m - q
    t20 = excel_round(q / params["target_cost_ratio"])
    return {
        "利益計算価格": m,
        "旧手数料": fee_old,
        "変動費合計": q,
        "旧利益額": old_profit,
        "旧利益率": (old_profit / m) if m else None,
        "利益20%価格": t20,
    }


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
    梱包サイズ変更のチェック（シート「梱包サイズ変更」B〜C列の再現）。
      利益チェック: 新サイズの送料・資材で利益率が margin_warn 以上か
      配送設定修正: 新旧で配送種別（宅配便/メール便）が変わるなら要修正
    返り値: dict(利益チェック, 配送設定要修正, 新利益額, 新利益率)
    """
    fee, q = variable_cost(cur_price, cost, shipping_new, material_new, params)
    m = profit_base_price(cur_price, delivery_old, params)
    profit = m - q
    margin = (profit / m) if m else None
    profit_ok = "〇" if (margin is not None and margin >= params["margin_warn"]) else "×"

    return {
        "利益チェック": profit_ok,
        "配送設定要修正": "要修正" if delivery_old != delivery_new else "不要",
        "新利益額": profit,
        "新利益率": margin,
    }
