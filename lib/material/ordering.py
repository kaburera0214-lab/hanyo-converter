# -*- coding: utf-8 -*-
"""
発注要否判定・発注数量の計算ロジック。

判定方針(確定):
  - 要発注:   現在庫 <= 発注点  （発注点ちょうども発注する）
  - 発注数量: 切上((在庫定数 - 現在庫) / ロット) * ロット を提案値とする
              （ロット未設定・0なら 在庫定数 - 現在庫 をそのまま提案）
  - 提案値は画面で手修正できる前提（あくまで初期値）

ロット候補(確定):
  - 「ロット候補」はカンマ区切り(全角／半角どちらも可)で複数持てる。先頭＝既定ロット。
  - 「単価」もロット候補に対応してカンマ区切りで持つ。個数が一致しないとエラー。
"""
import math
import re
import unicodedata


def _norm(s):
    """NFKC正規化(全角数字・全角カンマ→半角)。Noneは空文字。"""
    return unicodedata.normalize("NFKC", str(s)) if s is not None else ""


def to_num(v, default=0.0):
    """float文字列('4.0')やカンマ付きも吸収して数値化。空はdefault。"""
    if v is None:
        return default
    s = _norm(v).strip().replace(",", "")
    if s == "" or s.lower() in ("nan", "none"):
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def parse_num_list(s):
    """
    カンマ(半角/全角)・読点(、)区切りの数値文字列を float リストに。
    全角数字も吸収。数値化できない要素は無視する。
    """
    out = []
    for part in re.split(r"[,、]", _norm(s)):
        part = part.strip()
        if part == "":
            continue
        try:
            out.append(float(part))
        except ValueError:
            pass
    return out


def default_lot(lots_str):
    """ロット候補の先頭(既定ロット)。無ければ0。"""
    lots = parse_num_list(lots_str)
    return lots[0] if lots else 0.0


def lot_options(lots_str):
    """ロット候補を昇順を保たず元の順で返す(先頭=既定)。表示・選択用。"""
    return parse_num_list(lots_str)


def lot_price_ok(lots_str, prices_str):
    """
    ロット候補と単価の個数が一致するか。単価が空ならチェック対象外でTrue。
    単価が1つだけ・ロットが複数のときは『全ロット共通単価』とみなしTrue。
    """
    lots = parse_num_list(lots_str)
    prices = parse_num_list(prices_str)
    if not prices:
        return True
    if len(prices) == 1:
        return True
    return len(lots) == len(prices)


def price_for_lot(lots_str, prices_str, lot):
    """指定ロットに対応する単価。単価1つなら共通単価。見つからなければNone。"""
    lots = parse_num_list(lots_str)
    prices = parse_num_list(prices_str)
    if not prices:
        return None
    if len(prices) == 1:
        return prices[0]
    target = to_num(lot)
    for i, l in enumerate(lots):
        if abs(l - target) < 1e-9 and i < len(prices):
            return prices[i]
    return None


def needs_order(現在庫, 発注点):
    """現在庫 <= 発注点 なら要発注。発注点未設定(空)は判定対象外でFalse。"""
    s = str(発注点).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return False
    return to_num(現在庫) <= to_num(発注点)


def suggest_qty(現在庫, 在庫定数, lot):
    """発注数量の提案値。在庫定数までを指定ロット単位に切上げて補充。"""
    need = to_num(在庫定数) - to_num(現在庫)
    if need <= 0:
        return 0
    lotv = to_num(lot)
    if lotv <= 0:
        return int(math.ceil(need))
    return int(math.ceil(need / lotv) * lotv)
