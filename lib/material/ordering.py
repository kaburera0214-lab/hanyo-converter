# -*- coding: utf-8 -*-
"""
発注要否判定・発注数量の計算ロジック。

判定方針(確定):
  - 要発注:   現在庫 <= 発注点  （発注点ちょうども発注する）
  - 発注数量: 切上((在庫定数 - 現在庫) / ロット) * ロット を提案値とする
              （ロット未設定・0なら 在庫定数 - 現在庫 をそのまま提案）
  - 提案値は画面で手修正できる前提（あくまで初期値）
"""
import math


def to_num(v, default=0.0):
    """float文字列('4.0')やカンマ付きも吸収して数値化。空はdefault。"""
    if v is None:
        return default
    s = str(v).strip().replace(",", "")
    if s == "" or s.lower() in ("nan", "none"):
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def needs_order(現在庫, 発注点):
    """現在庫 <= 発注点 なら要発注。発注点未設定(空)は判定対象外でFalse。"""
    s = str(発注点).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return False
    return to_num(現在庫) <= to_num(発注点)


def suggest_qty(現在庫, 在庫定数, ロット):
    """発注数量の提案値。在庫定数までをロット単位に切上げて補充。"""
    need = to_num(在庫定数) - to_num(現在庫)
    if need <= 0:
        return 0
    lot = to_num(ロット)
    if lot <= 0:
        return int(math.ceil(need))
    return int(math.ceil(need / lot) * lot)
