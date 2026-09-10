# -*- coding: utf-8 -*-
"""Yahoo配送グループ待機キューの滞留判定。"""
import datetime

import pandas as pd

from lib.receiving import yahoo_queue as yq


def _df(stamps):
    return pd.DataFrame([{"code": f"c{i}", "配送グループ管理番号": "NM", "追加日時": s}
                         for i, s in enumerate(stamps)])


def test_oldest_age_uses_the_oldest_row():
    now = datetime.datetime.now()
    df = _df([(now - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
              (now - datetime.timedelta(days=40)).strftime("%Y-%m-%d %H:%M")])
    assert yq.oldest_age_days(df) == 40


def test_empty_queue_is_not_stale():
    assert yq.oldest_age_days(pd.DataFrame()) is None


def test_unreadable_timestamp_is_unknown_not_zero():
    """「読めない」を0日（＝正常）に丸めない。丸めると滞留が正常に見える。"""
    assert yq.oldest_age_days(_df(["", "-"])) is None
    assert yq.oldest_age_days(pd.DataFrame([{"code": "c1"}])) is None


def test_upload_csv_uses_yahoo_field_name_and_group_no():
    """アップ用CSVは半角フィールド名＋配送グループNo。

    2026-09-09: 見出し「配送グループ管理番号」／値NT・NMでアップして
    U-004-0020（フィールド名の誤り）で弾かれた。その形に戻らないよう固定する。
    """
    df = pd.DataFrame([{"code": "kira0001", "配送グループ管理番号": "NT", "追加日時": "2026-07-28 03:25"},
                       {"code": "artc4171", "配送グループ管理番号": "NM", "追加日時": "2026-09-09 05:11"}])
    csv = yq.delivery_upload_csv(df, {"宅配便": "3", "メール便": "5"}).decode("cp932")
    lines = csv.splitlines()
    assert lines[0] == "code,postage-set"
    assert lines[1] == "kira0001,3" and lines[2] == "artc4171,5"
    assert "配送グループ管理番号" not in csv and "NT" not in csv


def test_missing_group_no_blocks_csv():
    """Noが分からない便種は、空欄や推測値で埋めずに止める。"""
    df = pd.DataFrame([{"code": "kira0001", "配送グループ管理番号": "NT", "追加日時": ""}])
    assert yq.missing_group_bins(df, {"メール便": "5"}) == ["宅配便"]
    assert yq.missing_group_bins(df, {"宅配便": "3", "メール便": "5"}) == []
    try:
        yq.delivery_upload_csv(df, {"メール便": "5"})
        raise AssertionError("Noが無いのにCSVが作られた")
    except ValueError:
        pass
