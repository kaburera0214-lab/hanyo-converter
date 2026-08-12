# -*- coding: utf-8 -*-
"""
作業日を基準にした既定値（対象月・振込実行日）の計算。

  - 対象月     : 前月（8月に作業＝7月分を処理する運用）
  - 振込実行日 : 当月末日。土日祝・年末年始で銀行が動かない日は前営業日へ繰り上げ

祝日判定は jpholiday を使う。未導入の環境でも落とさず、土日と年末年始だけで
判定を続ける（画面上で日付を直せる前提の既定値のため）。
"""
import datetime

# 金融機関の休業日（土日・祝日に加えて年末年始）
_BANK_CLOSED_MMDD = {(12, 31), (1, 1), (1, 2), (1, 3)}


def is_holiday(d):
    """祝日か（jpholiday未導入なら常にFalse）。"""
    try:
        import jpholiday
        return bool(jpholiday.is_holiday(d))
    except Exception:  # noqa: BLE001 - 依存が無くても既定値計算は続ける
        return False


def is_bank_holiday(d):
    """銀行が動かない日（土日・祝日・12/31〜1/3）か。"""
    if d.weekday() >= 5:          # 土(5)・日(6)
        return True
    if (d.month, d.day) in _BANK_CLOSED_MMDD:
        return True
    return is_holiday(d)


def previous_business_day(d):
    """その日が銀行休業日なら、前の営業日まで遡った日付を返す。"""
    while is_bank_holiday(d):
        d -= datetime.timedelta(days=1)
    return d


def month_end(year, month):
    import calendar
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def default_exec_date(today=None):
    """振込実行日の既定＝作業日の当月末日（休業日なら前営業日）。"""
    today = today or datetime.date.today()
    return previous_business_day(month_end(today.year, today.month))


def default_exec_mmdd(today=None):
    """振込実行日の既定を楽天CSV用の MMDD 文字列で返す。"""
    d = default_exec_date(today)
    return f"{d.month:02d}{d.day:02d}"


def default_target_ym(today=None):
    """対象月の既定＝前月（'YYYY-MM'）。"""
    today = today or datetime.date.today()
    y, m = today.year, today.month - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y}-{m:02d}"
