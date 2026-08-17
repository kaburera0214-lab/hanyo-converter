# -*- coding: utf-8 -*-
"""
KPI計算（lib/qa/metrics）のテスト。
実行: hanyo-converter直下で  python tests/test_qa_metrics.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.qa import metrics as M  # noqa: E402
from lib.qa.history import JST  # noqa: E402

NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=JST)
_HAS_JPHOLIDAY = M.is_holiday(datetime.date(2026, 8, 11))  # 山の日


def q(番号, 日時, ステータス="完了", 履歴="", タイトル="質問"):
    return {"番号": 番号, "質問日時": 日時, "ステータス": ステータス,
            "編集履歴": 履歴, "タイトル": タイトル}


def test_営業日数():
    # 2026年7月は31日・土日8日 → 平日23日。海の日(7/20 月)を引いて22日
    expected_july = 22 if _HAS_JPHOLIDAY else 23
    assert M.business_days(2026, 7) == expected_july, M.business_days(2026, 7)
    # 8/13までで打ち切ると、8/3〜8/13の平日9日から山の日(8/11)を引いて8日
    got = M.business_days(2026, 8, until=datetime.date(2026, 8, 13))
    assert got == (8 if _HAS_JPHOLIDAY else 9), got


def test_日時のパース():
    assert M.parse_dt("2026-08-12T13:25:00.000+09:00").day == 12
    assert M.parse_dt("2026-08-12").month == 8
    assert M.parse_dt("") is None
    assert M.parse_dt(None) is None
    assert M.parse_dt("こわれた値") is None


def test_月次は起票月で数え追加質問は発生日で数える():
    questions = [
        # 7月に立って、8月に追加質問が2回
        q(1, "2026-07-30T10:00:00+09:00", 履歴="\n".join([
            "[2026-07-30 10:00] インハナ：質問投稿",
            "[2026-08-03 09:00] インハナ：追加質問",
            "[2026-08-05 09:00] インハナ：追加質問",
        ])),
        # 8月に立って追加質問なし
        q(2, "2026-08-04T10:00:00+09:00", 履歴="[2026-08-04 10:00] インハナ：質問投稿"),
    ]
    rows = {r["年月"]: r for r in M.monthly(questions, months=3, now=NOW)}
    assert rows["2026-07"]["質問数"] == 1
    assert rows["2026-08"]["質問数"] == 1
    # 追加質問は発生した8月に乗る（起票月の7月ではない）
    assert rows["2026-07"]["追加質問数"] == 0
    assert rows["2026-08"]["追加質問数"] == 2
    # ラリー率は起票月のコホート
    assert rows["2026-07"]["ラリー率"] == 100.0
    assert rows["2026-08"]["ラリー率"] == 0.0
    assert rows["2026-08"]["進行中"] is True
    assert rows["2026-07"]["進行中"] is False


def test_営業日あたりに換算する():
    questions = [q(i, "2026-07-06T10:00:00+09:00") for i in range(22)]
    row = [r for r in M.monthly(questions, months=2, now=NOW) if r["年月"] == "2026-07"][0]
    assert row["質問数"] == 22
    assert row["質問数_営業日"] == round(22 / row["営業日"], 2)


def test_滞留は待っている側で分かれる():
    questions = [
        q(1, "2026-07-22T12:00:00+09:00", ステータス="未回答", タイトル="古い未回答"),
        q(2, "2026-08-12T12:00:00+09:00", ステータス="再質問", タイトル="再質問"),
        q(3, "2026-07-22T12:00:00+09:00", ステータス="回答済", タイトル="完了待ち"),
        q(4, "2026-08-01T12:00:00+09:00", ステータス="完了", タイトル="終わり"),
    ]
    s = M.stalled(questions, now=NOW)
    assert [w["タイトル"] for w in s["パピー待ち"]] == ["古い未回答", "再質問"]  # 経過の長い順
    assert [w["タイトル"] for w in s["インハナ待ち"]] == ["完了待ち"]
    assert s["パピー待ち"][0]["経過日数"] == 22


def test_サマリーの前月差():
    questions = [q(1, "2026-08-04T10:00:00+09:00", ステータス="未回答"),
                 q(2, "2026-07-06T10:00:00+09:00")]
    s = M.summary(questions, now=NOW)
    assert s["当月"]["年月"] == "2026-08"
    assert s["前月"]["年月"] == "2026-07"
    assert s["パピー待ち"] == 1
    assert s["インハナ待ち"] == 0
    assert s["最長滞留日数"] == 9
    # 8月は営業日が少ないぶん「件/営業日」は7月より高くなる
    assert s["質問_前月差"] > 0


def test_空でも落ちない():
    s = M.summary([], now=NOW)
    assert s["当月"]["質問数"] == 0
    assert s["最長滞留日数"] == 0
    assert M.monthly([], months=3, now=NOW)[0]["ラリー率"] == 0.0


if __name__ == "__main__":
    if not _HAS_JPHOLIDAY:
        print("※ jpholiday が未導入のため、祝日を含むケースは土日のみで判定します")
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except AssertionError as e:
                fails += 1
                print(f"NG   {name}: {e}")
    print("---")
    print("全部通りました" if not fails else f"{fails}件 失敗")
    sys.exit(1 if fails else 0)
