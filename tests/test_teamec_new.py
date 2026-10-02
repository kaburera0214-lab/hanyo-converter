"""TeamEC新体系の金額境界・料金版と料金表の一致・旧体系との分岐を検証する。外部通信はしない。"""
import copy
import io
import json
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.invoice import teamec
from lib.invoice.teamec import rules as R

VER = R.version(2026, 9)


def event(name, **extra):
    return {"作業": name, "依頼ID": "REQ-1", "実施日": "2026-09-30", "数量": 1,
            "通知日": "", "時間外再手配": False, **extra}


# ---- 料金版と料金表（取得版）の一致：版だけ・表だけが変わったら落ちる ----

def test_every_version_price_appears_in_source_sheet():
    for name, values in VER["prices"].items():
        for v in values:
            assert f"{v:,}円" in R.ROWS[name][2], f"{name} {v}円が料金表にありません"
    for _, (item, v) in VER["storage"].items():
        assert f"{v:,}円" in R.ROWS[item][2]


def test_every_priced_sheet_row_is_in_version_or_explicitly_elsewhere():
    handled = set(VER["prices"]) | {item for item, _ in VER["storage"].values()} | set(VER["overrides"])
    # 出荷作業料はサイズ別（Eシス出荷実績で計算）、実費・管理費は料金表どおり別処理
    elsewhere = {"出荷作業料", "送料", "資材費", "着払い送料", "管理費"}
    for name, row in R.ROWS.items():
        if row[0] in ("保管", "第1層", "第2層", "第3層", "実費", "管理費"):
            assert name in handled | elsewhere, f"料金表の「{name}」が料金版にありません"


def test_only_overrides_differ_from_sheet():
    assert "40円" in R.ROWS["出荷指示作成料"][2]  # 料金表は40円/件
    assert R.dispatch(VER)["price"] == 1200          # 本人回答で上書き
    assert set(VER["overrides"]) == {"出荷指示作成料"}


def test_no_legacy_admin_fee_and_generic_labor_not_manual_event():
    assert not any("事務手数料" in n for n in R.EVENT_NAMES)
    assert "汎用作業料" not in R.EVENT_NAMES
    assert R.price("入庫：ピース納品", ver=VER) == 40


# ---- 分岐 ----

@pytest.mark.parametrize("client,year,month,expected", [
    ("Team-EC", 2026, 9, True), ("Team-EC", 2026, 12, True), ("Team-EC", 2027, 1, True),
    ("Team-EC", 2026, 8, False), ("未来", 2026, 9, False),
])
def test_routing_by_client_and_month(client, year, month, expected):
    assert teamec.applies(client, year, month) is expected


def test_old_months_rejected():
    with pytest.raises(ValueError, match="9月"):
        R.version(2026, 8)


# ---- 計算 ----

def test_dispatch_only_on_shipping_days_weekday_2_holiday_1():
    # 9/19土曜、9/21敬老の日、9/22国民の休日、9/23秋分の日
    days = ["2026-09-18", "2026-09-19", "2026-09-21", "2026-09-22", "2026-09-23"]
    lines = R.daily_lines(2026, 9, days)
    assert [x["金額"] for x in lines] == [(2 + 1 + 1 + 1 + 1) * 1200, 5 * 700, 5 * 1150]
    with pytest.raises(ValueError, match="重複"):
        R.daily_lines(2026, 9, days + days)


def test_management_fee_rounds_half_up_on_layer1():
    lines = [R.line("出荷作業料", 10010, 1, "第1層"), R.line("送料", 5000, 1, "実費")]
    result = R.totals(lines)
    assert result["items"][-1]["品名"] == "管理費"
    assert result["items"][-1]["金額"] == 501  # 500.5円 → 四捨五入


def test_support_is_deducted_last_without_changing_management_fee():
    lines = [R.line("出荷作業料", 10010, 1, "第1層"), R.line("送料", 5000, 1, "実費")]
    result = R.totals(lines, support=1000)
    names = [x["品名"] for x in result["items"]]
    assert names[-1] == "応援控除"
    assert next(x for x in result["items"] if x["品名"] == "管理費")["金額"] == 501
    assert result["subtotal"] == 10010 + 5000 + 501 - 1000
    with pytest.raises(ValueError, match="超えて"):
        R.totals(lines, support=20000)


def test_cooperation_fee_only_september():
    assert [(x["品名"], x["金額"]) for x in R.extra_lines(2026, 9)] == [("協力費", 20000)]
    assert R.extra_lines(2026, 10) == []
    result = R.totals(R.extra_lines(2026, 9))
    assert result["management_base"] == 0


def counts(*rows):
    return [{"期": p, "種別": k, "数量": q} for p, k, q in rows]


def test_storage_from_count_page_including_600_pallet():
    rows = counts(("第1期", "保管料：パレット", 2), ("第2期", "保管料：パレット", 4),
                  ("第1期", "保管料：中量棚", 1), ("第1期", "保管料：中量棚", 1), ("第2期", "保管料：中量棚", 4),
                  ("第1期", "保管料：当社指定ロケーション", 3), ("第2期", "保管料：当社指定ロケーション", 3))
    lines = R.storage_lines(rows, 2026, 9)
    assert [(x["品名"], x["金額"]) for x in lines] == [
        ("保管料（パレット）", 3000), ("保管料（中量棚）", 900), ("保管料（当社指定ロケーション）", 1800)]
    assert R.totals(lines)["management_base"] == 0


def test_storage_unknown_type_or_missing_period_is_error_not_zero():
    with pytest.raises(ValueError, match="料金にありません"):
        R.storage_lines(counts(("第1期", "保管料：謎", 1), ("第2期", "保管料：謎", 1)), 2026, 9)
    with pytest.raises(ValueError, match="第2期が未入力"):
        R.storage_lines(counts(("第1期", "保管料：パレット", 2)), 2026, 9)
    assert R.storage_lines([], 2026, 9) == []


def test_labor_uses_recorded_quarter_hours_without_rounding():
    rows = [{"日付": "2026/09/04", "作業項目": "JAN貼り", "時間数": 0.75, "人数": 2},
            {"日付": "2026/09/10", "作業項目": "入替", "時間数": 0.25, "人数": 1}]
    lines = R.labor_lines(rows, 2026, 9)
    assert lines[0]["数量"] == Decimal("1.75")
    assert lines[0]["金額"] == 3675
    assert lines[0]["区分"] == "第3層"
    with pytest.raises(ValueError, match="15分単位"):
        R.labor_lines([{"日付": "2026/09/04", "作業項目": "x", "時間数": 0.3, "人数": 1}], 2026, 9)
    assert R.labor_lines([], 2026, 9) == []


@pytest.mark.parametrize("qty,notice,expected", [
    (4, "2026-09-16", 2800), (5, "2026-09-16", 2250),
    (10, "2026-09-16", 3500), (10, "2026-09-17", 7000), (10, "", 7000),
])
def test_new_sku_volume_discount_is_per_request_and_14_days(qty, notice, expected):
    lines = R.event_lines([event("新商品 初期設定", 数量=qty, 通知日=notice)], 2026, 9)
    assert lines[0]["金額"] == expected


def test_extra_batch_and_vehicle_conditions():
    early = event("追加便対応料（3便目以降）", 通知日="2026-09-16")
    late = event("追加便対応料（3便目以降）", 通知日="2026-09-17")
    assert R.event_lines([early], 2026, 9)[0]["金額"] == 2000
    assert R.event_lines([late], 2026, 9)[0]["金額"] == 5500
    with pytest.raises(ValueError, match="事前協議"):
        R.event_lines([{**late, "数量": 2}], 2026, 9)
    assert R.event_lines([event("車両受入費")], 2026, 9)[0]["金額"] == 4000
    assert R.event_lines([event("車両受入費", 時間外再手配=True)], 2026, 9)[0]["金額"] == 7000


def test_fba_request_identity_not_boxes_and_no_duplicate_request():
    row = event("FBA対応費", 依頼ID="FBA0010")
    with pytest.raises(ValueError, match="重複"):
        R.event_lines([row, row], 2026, 9)
    with pytest.raises(ValueError, match="箱数"):
        R.event_lines([{**row, "数量": 2}], 2026, 9)
    result = R.totals(R.event_lines([row], 2026, 9))
    assert result["subtotal"] == 2000
    assert result["management_base"] == 0


@pytest.mark.parametrize("bad", ["", None, "NaN", "Infinity", "-1", "不明"])
def test_missing_or_invalid_values_never_become_zero(bad):
    with pytest.raises(ValueError):
        R.amount(bad)


def test_event_outside_period_and_return_double_count():
    with pytest.raises(ValueError, match="対象月外"):
        R.event_lines([event("返品処理", 実施日="2026-08-31")], 2026, 9)
    with pytest.raises(ValueError, match="返品処理に含まれ"):
        R.event_lines([event("返品処理"), event(R.STOCK_RESTORE)], 2026, 9)


# ---- 画面 ----

def _app_test():
    import importlib
    import streamlit
    if not hasattr(streamlit, "__version__"):
        for name in [n for n in sys.modules if n == "streamlit" or n.startswith("streamlit.")]:
            sys.modules.pop(name, None)
        importlib.import_module("streamlit")
    from streamlit.testing.v1 import AppTest
    return AppTest


SAMPLE_RESULT = {
    "対象月": "2026-09", "作成日時": "2026-10-02T15:00:00+09:00", "注文数": 3, "商品数": 4, "個口数": 2,
    "出荷作業料": 188, "出荷作業料_サイズ別": {"MB": 2, "60": 1}, "資材費": 113,
    "資材費_サイズ別": [{"サイズ": "MB", "個口数": 1, "単価": 37.14, "金額": 37}, {"サイズ": "60", "個口数": 1, "単価": 75.83, "金額": 76}],
    "送料合計": 900, "出荷稼働日": ["2026-09-01", "2026-09-19"],
    "FBA依頼": [{"注文番号": "FBA0010", "注文ID": "76441", "発送日": "2026-09-25"}],
    "入庫_ピース数": 10, "要確認": [], "棚番と項目1が違う商品": [],
}


def _results_dir(tmp_path, result=SAMPLE_RESULT):
    d = tmp_path / "results" / result["対象月"]
    d.mkdir(parents=True)
    (d / "20261002_150000_照合結果.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return str(tmp_path / "results")


def test_result_lines_and_month_check():
    lines = R.result_lines(SAMPLE_RESULT, VER)
    assert [(x["品名"], x["区分"], x["金額"]) for x in lines] == [
        ("出荷作業料", "第1層", 188), ("資材費", "実費", 113), ("送料", "実費", 900), ("入庫：ピース納品", "第1層", 400)]
    assert R.totals(lines)["management_base"] == 588
    with pytest.raises(ValueError, match="対象月"):
        R.check_result(SAMPLE_RESULT, 2026, 10)
    with pytest.raises(ValueError, match="必要な項目"):
        R.check_result({"対象月": "2026-09"}, 2026, 9)


def _page(monkeypatch, notion=True):
    AppTest = _app_test()
    from lib import auth
    from lib.invoice import store, notion_store
    baseline = copy.deepcopy(store.DEFAULT_CLIENTS)
    monkeypatch.setattr(auth, "require_role", lambda *a, **kw: None)
    monkeypatch.setattr(store, "load_clients", lambda: copy.deepcopy(baseline))

    def forbidden(*a, **kw):
        pytest.fail("試算表示でNotionへの書込みが呼ばれました")
    for name in ("save_issue_history", "save_storage_counts", "save_irregular_work",
                 "add_storage_count", "add_irregular_work"):
        monkeypatch.setattr(notion_store, name, forbidden)
    if notion:
        monkeypatch.setattr(notion_store, "ensure_databases", lambda: {k: k for k in notion_store.DB_SCHEMAS})
        for name in ("seed_clients_if_empty", "seed_area_map_if_empty", "seed_shipping_table_if_empty"):
            monkeypatch.setattr(notion_store, name, lambda *a, **kw: None)
        monkeypatch.setattr(notion_store, "load_clients", lambda db: copy.deepcopy(baseline))
        monkeypatch.setattr(notion_store, "load_storage_counts", lambda db, c, ym: counts(
            ("第1期", "保管料：パレット", 2), ("第2期", "保管料：パレット", 4)) if ym == "2026-09" else [])
        monkeypatch.setattr(notion_store, "load_irregular_work", lambda db, c, ym: [])
    page = Path(__file__).resolve().parents[1] / "pages" / "6_🧾_請求書発行.py"
    at = AppTest.from_file(str(page), default_timeout=30)
    at.secrets = {"INVOICE_NOTION_PARENT_PAGE_ID": "x" if notion else ""}
    at.session_state["invoice_client"] = "Team-EC"
    at.session_state["invoice_year"] = 2026
    at.session_state["invoice_month"] = 9
    return at, baseline, store


def test_team_ec_september_uses_new_system_and_keeps_old_for_august(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMEC_RESULTS_DIR", _results_dir(tmp_path))
    at, baseline, store = _page(monkeypatch)
    at.run()
    assert not at.exception, at.exception
    assert "TeamEC新体系" not in at.selectbox(key="invoice_client").options
    assert not at.error, [e.value for e in at.error]
    labels = [m.label for m in at.metric]
    assert "出荷作業料" in labels and "試算合計" in labels
    # 出荷稼働日は照合結果の日（9/19土曜は1回）：(2+1)×1,200
    items = at.dataframe[-1].value
    assert int(items.loc[items["品名"] == "出荷指示作成料", "金額"].iloc[0]) == 3600
    assert "FBA対応費" in set(items["品名"])
    at.checkbox(key=next(k for k in at.session_state.filtered_state if k.endswith("done_days"))).check().run()
    assert not at.exception
    assert not any("MF" in b.label and "取込不可" not in b.label for b in at.get("download_button"))
    next(n for n in at.number_input if n.label == "応援控除（税抜・円）").set_value(1000).run()
    assert not at.exception
    at.selectbox(key="invoice_month").select(8).run()
    assert not at.exception
    assert not any(m.label.startswith("試算") for m in at.metric)
    assert any("請求書ヘッダ" in h.value for h in at.header)
    at.selectbox(key="invoice_month").select(9).run()
    assert not at.exception
    assert next(n for n in at.number_input if n.label == "応援控除（税抜・円）").value == 1000
    at.selectbox(key="invoice_client").select("未来").run()
    assert not at.exception
    assert not any(m.label.startswith("試算") for m in at.metric)
    assert store.DEFAULT_CLIENTS == baseline


def test_without_notion_no_amount_is_shown(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMEC_RESULTS_DIR", _results_dir(tmp_path))
    at, _, _ = _page(monkeypatch, notion=False)
    at.run()
    assert not at.exception, at.exception
    assert not any(m.label.startswith("試算") for m in at.metric)
    assert any("0件扱いにはしません" in e.value for e in at.error)


def test_without_result_no_amount_is_shown(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMEC_RESULTS_DIR", str(tmp_path / "empty"))
    at, _, _ = _page(monkeypatch)
    at.run()
    assert not at.exception, at.exception
    assert not any(m.label.startswith("試算") for m in at.metric)
    assert any("照合結果" in e.value for e in at.error)


def test_review_zip_is_explicitly_draft_and_contains_version():
    _app_test()
    from lib.invoice.teamec.ui import review_bundle
    blob = review_bundle(R.totals(R.daily_lines(2026, 9, ["2026-09-01"])), {"実績CSV検証済": False}, VER)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert not any(n.startswith("MF") for n in z.namelist())
        data = json.loads(z.read("試算根拠.json"))
        assert data["status"] == "試算・請求不可"
        assert data["rate_version"]["id"] == "TE-2026-09"
        assert not data["inputs"]["実績CSV検証済"]
