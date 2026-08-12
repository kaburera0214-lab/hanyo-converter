# -*- coding: utf-8 -*-
"""対象月・振込実行日の既定値（営業日計算）のテスト。"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.payable import business_day as BD  # noqa: E402

_HAS_JPHOLIDAY = BD.is_holiday(datetime.date(2026, 8, 11))  # 山の日


def test_対象月は前月():
    assert BD.default_target_ym(datetime.date(2026, 8, 13)) == "2026-07"
    assert BD.default_target_ym(datetime.date(2026, 1, 5)) == "2025-12"


def test_振込実行日は当月末日():
    """2026/08/31は月曜のため、そのまま0831。"""
    assert BD.default_exec_mmdd(datetime.date(2026, 8, 13)) == "0831"


def test_末日が土日なら前営業日に繰り上がる():
    # 2026/05/31は日曜 → 金曜の5/29
    assert BD.default_exec_date(datetime.date(2026, 5, 1)) == datetime.date(2026, 5, 29)
    # 2026/01/31は土曜 → 金曜の1/30
    assert BD.default_exec_mmdd(datetime.date(2026, 1, 10)) == "0130"


def test_年末は銀行休業日を避ける():
    """12/31は銀行が動かないので12/30（平日）へ。"""
    assert BD.default_exec_date(datetime.date(2025, 12, 1)) == datetime.date(2025, 12, 30)
    assert BD.is_bank_holiday(datetime.date(2026, 1, 2)) is True


def test_祝日判定():
    if not _HAS_JPHOLIDAY:
        return  # jpholiday未導入の環境では土日のみで判定する
    assert BD.is_bank_holiday(datetime.date(2026, 8, 11)) is True   # 山の日
    assert BD.is_bank_holiday(datetime.date(2026, 8, 12)) is False
