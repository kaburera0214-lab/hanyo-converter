"""新体系の金額境界、二重課金、旧UIとの分離を検証する。外部通信はしない。"""
import copy
import io
import json
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.invoice.teamec import rules as R


def event(name, **extra):
    return {"作業": name, "依頼ID": "REQ-1", "実施日": "2026-09-30", "数量": 1,
            "通知日": "", "分/人": 0, "人数": 1, "時間外再手配": False, **extra}


def test_holiday_flat_batches_and_separate_management_layers():
    # 9/21敬老の日、9/22国民の休日、9/23秋分の日を祝日として扱う。
    days = ["2026-09-18", "2026-09-19", "2026-09-21", "2026-09-22", "2026-09-23"]
    lines = R.daily_lines(2026, 9, days)
    assert [x["金額"] for x in lines] == [7200, 3500, 5750]
    result = R.totals(lines)
    assert result["management_base"] == 10700
    assert result["items"][-1]["金額"] == 535
    assert result["subtotal"] == 16985
    with pytest.raises(ValueError, match="9月"):
        R.daily_lines(2026, 8, [])
    with pytest.raises(ValueError, match="重複"):
        R.daily_lines(2026, 9, days + days)


def test_storage_special_600_does_not_change_normal_or_management():
    rows = [{"種別": name, "15日": 2, "月末": 4} for name in R.STORAGE_NAMES]
    lines = R.storage_lines(rows)
    assert [x["金額"] for x in lines] == [3000, 900, 1800]
    result = R.totals(lines, storage_discount=100)
    assert result["management_base"] == 0
    assert result["subtotal"] == 5600


def test_current_source_rates_and_no_legacy_admin():
    # 実装と画面の説明は同じ保存版を読む。本人回答の回数型だけが表への差分。
    assert R.price("入庫：ピース納品") == 40
    assert R.price("入庫：ケース納品") == 80
    assert R.price("入庫：パレット納品") == 500
    assert "5%" in R.ROWS["管理費"][2]
    assert R.MANAGEMENT_RATE == Decimal("0.05")
    assert not any("事務手数料" in n for n in R.EVENT_NAMES)
    assert R.SOURCE["checked_at"] == "2026-10-02"
    assert R.event_unit("入庫：パレット納品") == "PL"
    assert R.event_unit("入庫：ケース納品") == "箱"
    assert R.event_unit("入庫：ピース納品") == "pcs"
    assert R.event_unit("FBA対応費") == "依頼"


@pytest.mark.parametrize("qty,notice,expected", [
    (4, "2026-09-16", 2800), (5, "2026-09-16", 2250),
    (10, "2026-09-16", 3500), (10, "2026-09-17", 7000), (10, "", 7000),
])
def test_new_sku_volume_discount_is_per_request_and_14_days(qty, notice, expected):
    lines = R.event_lines([event("新商品 初期設定", 数量=qty, 通知日=notice)], R.Policy(), 2026, 9)
    assert lines[0]["金額"] == expected


def test_generic_person_minutes_rounding_alternatives():
    rows = [event("汎用作業料", 人数=2, **{"分/人": 20})]
    assert R.event_lines(rows, R.Policy(), 2026, 9)[0]["金額"] == 1575
    assert R.event_lines(rows, R.Policy(labor_rounding="各人ごと"), 2026, 9)[0]["金額"] == 2100
    assert R.event_lines([event("汎用作業料", 人数=2, **{"分/人": 10})], R.Policy(), 2026, 9)[0]["金額"] == 1050


def test_extra_batch_and_vehicle_conditions():
    early = event("追加便対応料（3便目以降）", 通知日="2026-09-16")
    late = event("追加便対応料（3便目以降）", 通知日="2026-09-17")
    assert R.event_lines([early], R.Policy(), 2026, 9)[0]["金額"] == 2000
    assert R.event_lines([late], R.Policy(), 2026, 9)[0]["金額"] == 5500
    with pytest.raises(ValueError, match="事前協議"):
        R.event_lines([{**late, "数量": 2}], R.Policy(), 2026, 9)
    assert R.event_lines([event("車両受入費")], R.Policy(), 2026, 9)[0]["金額"] == 4000
    assert R.event_lines([event("車両受入費", 時間外再手配=True)], R.Policy(), 2026, 9)[0]["金額"] == 7000


def test_fba_request_identity_not_boxes_and_no_duplicate_request():
    row = event("FBA対応費")
    with pytest.raises(ValueError, match="重複"):
        R.event_lines([row, row], R.Policy(), 2026, 9)
    with pytest.raises(ValueError, match="箱数"):
        R.event_lines([{**row, "数量": 3}], R.Policy(), 2026, 9)
    result = R.totals(R.event_lines([row], R.Policy(), 2026, 9))
    assert result["subtotal"] == 2000
    assert result["management_base"] == 0


def test_support_and_rounding_are_explicit():
    lines = [R.line("出荷作業料", 10010, 1, "第1層"), R.line("送料", 5000, 1, "実費")]
    assert R.totals(lines)["items"][-1]["金額"] == 501
    assert R.totals(lines, policy=R.Policy(management_rounding="切り捨て"))["items"][-1]["金額"] == 500
    assert R.totals(lines, support=1000)["items"][-1]["金額"] == 451
    assert R.totals(lines, support=1000, policy=R.Policy(support_reduces_base=False))["items"][-1]["金額"] == 501
    with pytest.raises(ValueError, match="控除額"):
        R.totals(lines, support=10011)


@pytest.mark.parametrize("bad", ["", None, "NaN", "Infinity", "-1", "不明"])
def test_missing_or_invalid_values_never_become_zero(bad):
    with pytest.raises(ValueError):
        R.amount(bad)


def test_event_outside_period_is_rejected():
    with pytest.raises(ValueError, match="対象月外"):
        R.event_lines([event("返品処理", 実施日="2026-08-31")], R.Policy(), 2026, 9)


def test_return_already_includes_stock_restore():
    with pytest.raises(ValueError, match="返品処理に含まれ"):
        R.event_lines([event("返品処理"), event("在庫調整（在庫戻し／一時在庫へ移動）")], R.Policy(), 2026, 9)


def _app_test():
    import importlib
    import streamlit
    if not hasattr(streamlit, "__version__"):
        for name in [n for n in sys.modules if n == "streamlit" or n.startswith("streamlit.")]:
            sys.modules.pop(name, None)
        importlib.import_module("streamlit")
    from streamlit.testing.v1 import AppTest
    return AppTest


def test_new_and_old_invoice_pages_render_and_keep_state_separate(monkeypatch):
    AppTest = _app_test()
    from lib import auth
    from lib.invoice import store, notion_store
    baseline = copy.deepcopy(store.DEFAULT_CLIENTS)
    monkeypatch.setattr(auth, "require_role", lambda *a, **kw: None)
    monkeypatch.setattr(store, "load_clients", lambda: copy.deepcopy(baseline))
    def forbidden(*a, **kw):
        pytest.fail("試算表示でNotionへの書込みが呼ばれました")
    monkeypatch.setattr(notion_store, "ensure_databases", forbidden)
    monkeypatch.setattr(notion_store, "save_issue_history", forbidden)
    page = Path(__file__).resolve().parents[1] / "pages" / "6_🧾_請求書発行.py"
    at = AppTest.from_file(str(page), default_timeout=30)
    at.secrets = {"INVOICE_NOTION_PARENT_PAGE_ID": ""}
    at.session_state["invoice_client"] = "TeamEC新体系"
    at.session_state["invoice_year"] = 2026
    at.session_state["invoice_month"] = 9
    at.run()
    assert not at.exception
    assert "TeamEC新体系" in at.selectbox(key="invoice_client").options
    assert len(at.metric) == 3
    assert not any("MF請求書CSV" in b.label for b in at.get("download_button"))
    next(n for n in at.number_input if n.label == "応援控除（税抜・円）").set_value(1000).run()
    assert not at.exception
    at.selectbox(key="invoice_client").select("Team-EC").run()
    assert not at.exception
    assert not any(m.label.startswith("試算") for m in at.metric)
    assert any("請求書ヘッダ" in h.value for h in at.header)
    at.selectbox(key="invoice_client").select("未来").run()
    assert not at.exception
    assert not any(m.label.startswith("試算") for m in at.metric)
    at.selectbox(key="invoice_client").select("TeamEC新体系").run()
    assert not at.exception
    assert next(n for n in at.number_input if n.label == "応援控除（税抜・円）").value == 1000
    assert store.DEFAULT_CLIENTS == baseline
    at.selectbox(key="invoice_month").select(10).run()
    assert not at.exception
    assert next(n for n in at.number_input if n.label == "応援控除（税抜・円）").value == 0
    at.selectbox(key="invoice_month").select(9).run()
    assert not at.exception
    assert next(n for n in at.number_input if n.label == "応援控除（税抜・円）").value == 1000
    at.selectbox(key="invoice_month").select(8).run()
    assert not at.exception
    assert not at.metric
    assert any("8月以前" in w.value for w in at.warning)


def test_review_zip_is_explicitly_draft_and_contains_source():
    _app_test()
    from lib.invoice.teamec.ui import review_bundle
    blob = review_bundle(R.totals(R.daily_lines(2026, 9, ["2026-09-01"])), {"実績CSV検証済": False})
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert not any(n.startswith("MF") for n in z.namelist())
        data = json.loads(z.read("試算根拠.json"))
        assert data["status"] == "試算・請求不可"
        assert data["source"]["checked_at"] == "2026-10-02"
        assert not data["inputs"]["実績CSV検証済"]
