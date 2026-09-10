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


def _ok(postage, category="2500"):
    """getItem が読めたときの形。category="" はプロダクトカテゴリ未設定。"""
    return {"state": yitems.STATE_OK, "postage_set": postage,
            "product_category": category, "message": ""}


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
    current = {"nssk0098": {"state": yitems.STATE_NOT_FOUND, "postage_set": None,
                            "product_category": None, "message": "無い"}}
    plan = ydv.classify([{"code": "nssk0098", "便種": "メール便"}],
                        current, GROUP_NO, GROUP_BINS)
    assert plan.to_update == [] and [r["code"] for r in plan.not_found] == ["nssk0098"]


def test_unreadable_current_value_is_not_guessed():
    """現在値を読めなかったものは推測で書き換えない（読めない＝不明）。"""
    current = {"x0001": {"state": yitems.STATE_ERROR, "postage_set": None,
                         "product_category": None, "message": "通信失敗"}}
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
    assert plan.to_update == [{"code": "x0003", "便種": "メール便", "no": "2",
                               "現在No": "", "カテゴリ": "2500"}]


def test_upload_csv_shape():
    """送るCSVは半角フィールド名＋配送グループNo。"""
    csv = ydv.upload_csv([{"code": "kawa1370", "便種": "宅配便", "no": "1",
                           "現在No": "2", "カテゴリ": "2500"}])
    lines = csv.decode("cp932").splitlines()
    assert lines[0] == "code,postage-set" and lines[1] == "kawa1370,1"


def test_category_is_filled_in_a_separate_csv():
    """カテゴリ未設定の行だけ product-category を足し、別CSVに分ける。

    項目指定は空欄を送ると値が消えるので、カテゴリのある行と無い行を
    1枚に混ぜられない（混ぜると、設定済みのカテゴリを消しにいく）。
    """
    to_update = [
        {"code": "kawa1370", "便種": "宅配便", "no": "1", "現在No": "2", "カテゴリ": "2500"},
        {"code": "artc4168", "便種": "メール便", "no": "2", "現在No": "1", "カテゴリ": ""},
    ]
    assert ydv.needs_category(to_update) == ["artc4168"]

    batches = ydv.upload_batches(to_update, {"artc4168": "13457"})
    assert len(batches) == 2
    plain = next(b for b in batches if not b["with_category"])
    withcat = next(b for b in batches if b["with_category"])
    assert plain["csv"].decode("cp932").splitlines()[0] == "code,postage-set"
    assert plain["codes"] == ["kawa1370"]
    lines = withcat["csv"].decode("cp932").splitlines()
    assert lines[0] == "code,postage-set,product-category"
    assert lines[1] == "artc4168,2,13457"


def test_uninferable_category_row_is_not_sent():
    """カテゴリを推定できなかった行は送らない（送っても弾かれるだけ）。"""
    to_update = [{"code": "artc4168", "便種": "メール便", "no": "2",
                  "現在No": "1", "カテゴリ": ""}]
    assert ydv.upload_batches(to_update, {}) == []


def test_not_found_is_reported_as_unregistered(monkeypatch):
    """getItem の HTTP 400 は「Yahoo未登録」として扱う（2026-09-10 nssk0098 で確認）。

    「読めなかった」に丸めると、登録しない限り解消しないものを反映待ちとして
    抱え続けることになる。
    """
    import requests

    from lib.yahoo_api import category_repair, client

    class _Res:
        status_code = 400
        content = (b'<?xml version="1.0" encoding="UTF-8" ?>'
                   b'<Error><Message>Bad Request</Message></Error>')

    monkeypatch.setattr(client, "access_token", lambda: "dummy")
    monkeypatch.setattr(client, "seller_id", lambda: "seller")
    monkeypatch.setattr(category_repair, "_rate_limit", lambda _k: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Res())

    try:
        category_repair.get_item("nssk0098")
        raise AssertionError("例外にならなかった")
    except category_repair.YahooItemNotFound as e:
        assert "登録されていません" in str(e)
        assert "Bad Request" in str(e)      # 応答本文は丸めずに残す


def test_publish_busy_is_pending_not_failure(monkeypatch):
    """反映予約の ed-00006 は失敗ではなく順番待ち（アップロード直後は必ず出る）。"""
    from lib.yahoo_api import items as yitems

    calls = []

    def _busy():
        calls.append(1)
        return ["Yahoo APIエラー HTTP 400: <Code>ed-00006</Code>"
                "<Message>反映またはアップロード中のため更新ができません。</Message>"]

    monkeypatch.setattr(yitems, "reserve_publish", _busy)
    ok, busy, errs = yitems.reserve_publish_retry(attempts=2, wait_seconds=0)
    assert (ok, busy) == (False, True) and len(calls) == 2
    assert errs and "ed-00006" in errs[0]


def test_publish_succeeds_after_waiting(monkeypatch):
    """待てば通る場合は成功として返す（1回目で諦めない）。"""
    from lib.yahoo_api import items as yitems

    state = {"n": 0}

    def _busy_then_ok():
        state["n"] += 1
        if state["n"] == 1:
            return ["<Code>ed-00006</Code>"]
        return []

    monkeypatch.setattr(yitems, "reserve_publish", _busy_then_ok)
    assert yitems.reserve_publish_retry(attempts=3, wait_seconds=0) == (True, False, [])


def test_real_publish_error_is_not_treated_as_busy(monkeypatch):
    """ed-00006 以外は保留にせず、失敗として返す（黙って待ち続けない）。"""
    from lib.yahoo_api import items as yitems

    monkeypatch.setattr(yitems, "reserve_publish", lambda: ["<Code>pm-05005</Code>"])
    ok, busy, errs = yitems.reserve_publish_retry(attempts=3, wait_seconds=0)
    assert (ok, busy) == (False, False) and errs == ["<Code>pm-05005</Code>"]


def test_nothing_to_publish_is_success(monkeypatch):
    """pm-05001「未反映項目はありません」は失敗ではなく、反映済みの意味。

    2026-09-10: これを失敗として扱っていたため、実際は反映が終わっているのに
    画面が「反映予約に失敗」と言い続け、原因の切り分けを何往復もさせてしまった。
    """
    from lib.yahoo_api import items as yitems

    monkeypatch.setattr(yitems, "reserve_publish",
                        lambda: ["<Code>pm-05001</Code><Message>未反映項目はありません</Message>"])
    assert yitems.reserve_publish_retry(attempts=3, wait_seconds=0) == (True, False, [])


def test_publish_busy_covers_both_codes(monkeypatch):
    """順番待ちは ed-00006（アップロード中）と pm-05002（反映処理中）の両方。"""
    from lib.yahoo_api import items as yitems

    for code in ("ed-00006", "pm-05002"):
        monkeypatch.setattr(yitems, "reserve_publish", lambda c=code: [f"<Code>{c}</Code>"])
        ok, busy, _ = yitems.reserve_publish_retry(attempts=2, wait_seconds=0)
        assert (ok, busy) == (False, True), code


def test_response_is_decoded_as_utf8_not_latin1():
    """日本語のエラーメッセージが化けないこと（化けると原因が読めない）。"""
    from lib.yahoo_api import client

    class _Res:
        content = "反映またはアップロード中のため更新ができません。".encode("utf-8")

    assert client.decode(_Res()) == "反映またはアップロード中のため更新ができません。"


def test_category_zero_counts_as_unset():
    """Yahooは未設定を "0" で返す。空文字だけを見ると補完が起動しない。

    2026-09-10: この取りこぼしで artc4168 の補完が一度も走らず、
    U-001-0363 のまま反映されない状態が続いた。
    """
    assert ydv.has_category("13457") is True
    for unset in ("0", "", "   ", None, "abc"):
        assert ydv.has_category(unset) is False, unset

    to_update = [{"code": "artc4168", "便種": "メール便", "no": "2",
                  "現在No": "1", "カテゴリ": "0"}]
    assert ydv.needs_category(to_update) == ["artc4168"]


def test_force_category_ignores_existing_value():
    """廃止済みIDが入っている商品は、既存値を無視して推定し直せること。"""
    to_update = [{"code": "artc4168", "便種": "メール便", "no": "2",
                  "現在No": "1", "カテゴリ": "99999"}]
    assert ydv.needs_category(to_update) == []
    assert ydv.needs_category(to_update, force=True) == ["artc4168"]


def test_query_variants_broaden_progressively():
    """検索語は「全部→減らす」で広げる。狭いまま0件で諦めない。"""
    from lib.yahoo_api import shop_category as ycat

    assert ycat._query_variants("フェルト ワンピース イエロー Jサイズ 衣装ベース") == [
        "フェルト ワンピース イエロー", "フェルト ワンピース", "フェルト"]
    assert ycat._query_variants("フェルト") == ["フェルト"]
    assert ycat._query_variants("") == []


def test_descend_to_leaf_returns_product_category_id(monkeypatch):
    """末端(IsLeaf=1)のCategoryCodeだけがプロダクトカテゴリID。

    2026-09-10: 途中のSHPカテゴリコードを product-category に書いていたため、
    U-001-0363「プロダクトカテゴリが存在しません」が消えなかった。
    """
    from lib.yahoo_api import shop_category as ycat

    tree = {
        "100": [{"code": "200", "name": "衣装", "is_leaf": False}],
        "200": [{"code": "14519", "name": "コスプレ衣装", "is_leaf": True}],
    }
    monkeypatch.setattr(ycat, "children", lambda code=None: tree.get(str(code), []))
    leaf = ycat.descend_to_leaf("100")
    assert leaf["category_id"] == 14519 and leaf["category_name"] == "コスプレ衣装"
    assert ycat.descend_to_leaf("999") is None       # 子が無いものはIDにしない
