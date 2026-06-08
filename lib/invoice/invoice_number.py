# -*- coding: utf-8 -*-
"""
請求書番号の採番・日付計算。

サンプル準拠：
  請求書番号 = YYMMDD（請求日＝対象作業月の末日）-クライアント略号   例) 260531-TE
  請求日     = 対象作業月の末日                                      例) 2026/5/31
  支払期限   = 翌月末日                                              例) 2026/6/30
いずれも画面で上書き可能にする前提の「初期値生成」関数群。
"""
import calendar
from datetime import date


def month_end(year, month):
    """指定年月の末日を返す。"""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


def next_month_end(year, month):
    """翌月の末日を返す。"""
    if month == 12:
        return month_end(year + 1, 1)
    return month_end(year, month + 1)


def format_ymd_slash(d):
    """2026/5/31 形式（ゼロ埋めなし）。MFサンプルの表記に合わせる。"""
    return f"{d.year}/{d.month}/{d.day}"


def generate_invoice_number(year, month, client_code):
    """
    対象作業月(year, month)とクライアント略号から請求書番号を生成する。
    末日を YYMMDD にして「-略号」を付ける。例) 2026年5月, TE -> 260531-TE
    """
    d = month_end(year, month)
    return f"{d:%y%m%d}-{client_code}"


def default_dates(year, month):
    """
    対象作業月から 請求日・支払期限・売上計上日 の初期値を返す（slash表記の文字列）。
    """
    issue = month_end(year, month)        # 請求日＝対象月末日
    due = next_month_end(year, month)     # 支払期限＝翌月末日
    return {
        "請求日": format_ymd_slash(issue),
        "お支払期限": format_ymd_slash(due),
        "売上計上日": format_ymd_slash(issue),
    }
