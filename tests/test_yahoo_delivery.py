# -*- coding: utf-8 -*-
"""
Yahoo配送グループの反映判定（本当に変える必要があるものだけを選ぶ）。

2026-09-09の実データで起きたことをそのまま固定する:
  kira0001 … Yahoo側は No.3「宅配便(YT)」。便種はすでに宅配便なので変更不要なのに、
              既定の No.1「宅配便(NT)」を書き込む＝キャリアだけ変わるところだった
  nssk0098 … Yahooに商品が無く、CSVアップが U-001-0300（新規商品のため必須フィールド）で失敗
  artc4171 … Yahoo側が元からメール便。楽天・NEと食い違っていただけ
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.receiving import yahoo_delivery as ydv   # noqa: E402
from lib.yahoo_api import items as yitems         # noqa: E402

GROUP_NO = {"宅配便": "1", "メール便": "2"}
GROUP_BINS = {"1": "宅配便", "2": "メール便", "3": "宅配便",
              "4": "宅配便", "5": "メール便", "6": "宅配便"}


def _ok(value):
    return {"state": yitems.STATE_OK, "value": value, "message": ""}


def test_same_bin_different_carrier_is_left_alone():
    """すでに同じ便種の別グループにいる商品は触らない（キャリアを勝手に変えない）。"""
    rows = [{"code": "kira0001", "便種": "宅配便"}]
    plan = ydv.classify(rows, {"kira0001": _ok("3")}, GROUP_NO, GROUP_BINS)
    assert plan.to_update == []
    assert plan.already == [{"code": "kira0001", "便種": "宅配便", "現在No": "3"}]


def test_actual_bin_change_is_updated():
    """便種が変わる商品だけを更新対象にする。"""
    rows = [{"code": "kawa1370", "便種": "宅配便"},     # 現在メール便(2) → 宅配便
            {"code": "artc4168", "便種": "メール便"}]   # 現在宅配便(1) → メール便
    plan = ydv.classify(rows, {"kawa1370": _ok("2"), "artc4168": _ok("1")},
                        GROUP_NO, GROUP_BINS)
    assert [r["code"] for r in plan.to_update] == ["kawa1370", "artc4168"]
    assert [r["no"] for r in plan.to_update] == ["1", "2"]


def test_already_correct_group_is_skipped():
    """artc4171 のように元から目的の便種なら対象外。"""
    plan = ydv.classify([{"code": "artc4171", "便種": "メール便"}],
                        {"artc4171": _ok("2")}, GROUP_NO, GROUP_BINS)
    assert plan.to_update == [] and len(plan.already) == 1


def test_unregistered_item_is_reported_not_queued_forever():
    """Yahoo未登録は「未反映」ではなく「未登録」。待っても解消しないので分けて出す。"""
    current = {"nssk0098": {"state": yitems.STATE_NOT_FOUND, "value": None, "message": "無い"}}
    plan = ydv.classify([{"code": "nssk0098", "便種": "メール便"}],
                        current, GROUP_NO, GROUP_BINS)
    assert plan.to_update == [] and [r["code"] for r in plan.not_found] == ["nssk0098"]


def test_unreadable_current_value_is_not_guessed():
    """現在値を読めなかったものは推測で書き換えない（読めない＝不明）。"""
    current = {"x0001": {"state": yitems.STATE_ERROR, "value": None, "message": "通信失敗"}}
    plan = ydv.classify([{"code": "x0001", "便種": "宅配便"}], current, GROUP_NO, GROUP_BINS)
    assert plan.to_update == [] and len(plan.unknown) == 1


def test_unknown_group_number_is_not_guessed():
    """対応表に無い配送グループNoは、どの便種か分からないので触らない。"""
    plan = ydv.classify([{"code": "x0002", "便種": "宅配便"}],
                        {"x0002": _ok("9")}, GROUP_NO, GROUP_BINS)
    assert plan.to_update == [] and "9" in plan.unknown[0]["理由"]


def test_empty_group_gets_the_target_group():
    """配送グループ未設定の商品には、目的の便種のグループを入れる（奪うキャリアが無い）。"""
    plan = ydv.classify([{"code": "x0003", "便種": "メール便"}],
                        {"x0003": _ok("")}, GROUP_NO, GROUP_BINS)
    assert plan.to_update == [{"code": "x0003", "便種": "メール便", "no": "2", "現在No": ""}]


def test_upload_csv_shape():
    """送るCSVは半角フィールド名＋配送グループNo。"""
    csv = ydv.upload_csv([{"code": "kawa1370", "便種": "宅配便", "no": "1", "現在No": "2"}])
    lines = csv.decode("cp932").splitlines()
    assert lines[0] == "code,postage-set" and lines[1] == "kawa1370,1"
