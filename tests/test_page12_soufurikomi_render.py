# -*- coding: utf-8 -*-
"""
振込CSV生成ページ（`pages/12`）の②総合振込仕訳帳CSVが、実際に描画できるかを見る。

このページはNotionの認証情報が無いと初期化で止まるため、ローカルでは画面を開けない。
lib/payable の入口だけ差し替えて、ページ本体を最後まで走らせる。
単体テストでは拾えない「画面側の書き間違い（未定義の変数・列名の取り違え）」を止めるのが目的。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _app_test():
    """
    AppTest を取り出す。

    tests/test_auth_keepalive.py が sys.modules["streamlit"] をヘッドレスシムに
    差し替えるため、収集順によっては本物のstreamlitが居ないことがある。
    その場合は本物を読み直してから取る（import時に解決すると収集ごと落ちる）。
    """
    import importlib
    import streamlit
    if not hasattr(streamlit, "__version__"):       # シムが入っている
        for name in [n for n in sys.modules if n == "streamlit" or n.startswith("streamlit.")]:
            sys.modules.pop(name, None)
        importlib.import_module("streamlit")
    from streamlit.testing.v1 import AppTest
    return AppTest


PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "pages", "12_💴_-_振込CSV生成.py")

# 取引先マスタ（Notionの1行ぶん）。②に必要な列だけ持たせる
_MASTER = {
    "取引先ア": {
        "会社名": "取引先ア", "支払区分": "銀行振込", "支払方法": "振込",
        "銀行番号": "0001", "支店番号": "001", "預金種目": "普通", "口座番号": "1234567",
        "受取人口座名": "カ）トリヒキサキア", "除外フラグ": "",
        "借方勘定科目": "仕入高", "借方補助科目": "取引先ア", "借方税区分": "課税仕入 10%",
        "貸方勘定科目": "買掛金", "貸方補助科目": "取引先ア", "貸方税区分": "対象外",
        "摘要": "取引先ア", "MF並び順": 1, "源泉税額": "",
    },
    "社労士オ": {
        "会社名": "社労士オ", "支払区分": "銀行振込", "支払方法": "振込",
        "銀行番号": "0005", "支店番号": "002", "預金種目": "普通", "口座番号": "7654321",
        "受取人口座名": "シヤロウシオ", "除外フラグ": "",
        "借方勘定科目": "支払報酬", "借方補助科目": "", "借方税区分": "課税仕入 10%",
        "貸方勘定科目": "", "貸方補助科目": "", "貸方税区分": "",
        "摘要": "社労士オ", "MF並び順": 2, "源泉税額": 1021,
    },
}

_INVOICES = [
    {"会社名": "取引先ア", "当月請求額": 76038, "ステータス": "確認済",
     "突合状態": "一致", "軽減税率": False},
    {"会社名": "社労士オ", "当月請求額": 9979, "ステータス": "確認済",
     "突合状態": "一致", "軽減税率": False},
]


@pytest.fixture
def page(monkeypatch):
    from lib import auth
    from lib.payable import app_init, notion_payable as N

    monkeypatch.setattr(auth, "require_role", lambda *a, **k: None)
    monkeypatch.setattr(app_init, "init_payable", lambda *a, **k: {"支払_取引先マスタ": "x"})
    monkeypatch.setattr(N, "load_master", lambda *a, **k: list(_MASTER.values()))
    monkeypatch.setattr(N, "load_invoices", lambda *a, **k: list(_INVOICES))
    at = _app_test().from_file(PAGE, default_timeout=30)
    at.session_state["csv_done_ym"] = "2026-05"     # 楽天CSV生成済みの状態にする
    return at


def _run(at, **state):
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, at.exception
    return at


def test_ページが例外なく描画される(page):
    at = _run(page, payable_target_ym="2026-05", payable_exec="0630")
    texts = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "② 総合振込仕訳帳CSV（振込実行分の支払）" in texts


def test_取引日の既定が振込実行日になる(page):
    at = _run(page, payable_target_ym="2026-05", payable_exec="0630")
    date_inputs = [t for t in at.text_input if t.key == "mf_sf_date"]
    assert date_inputs and date_inputs[0].value == "2026/06/30"
    # ①は対象月の末日。1ヶ月ずれているのが正しい
    assert [t for t in at.text_input if t.key == "mf_kk_date"][0].value == "2026/05/31"


def test_取引Noが空の間はダウンロードできない(page):
    """経理シートの連番を入れるまで押せない（既定値を入れて事故らせない）。"""
    at = _run(page, payable_target_ym="2026-05", payable_exec="0630")
    btns = [b for b in at.button if b.key == "mf_sf_dl_off"]
    assert btns and btns[0].disabled


def test_取引Noを入れるとDLボタンが出て源泉が仕訳に載る(page):
    at = _run(page, payable_target_ym="2026-05", payable_exec="0630", mf_sf_no="164")
    # AppTestに download_button の専用アクセサが無いので element 名で拾う
    labels = [b.label for b in at.get("download_button")]
    assert "📥 総合振込仕訳帳CSVをダウンロード" in labels, labels
    assert not [b for b in at.button if b.key == "mf_sf_dl_off"], "無効ボタンが残っている"
    texts = " ".join(m.value for m in at.markdown)
    # 仮受金＝振込総額(76,038+9,979)、借方合計＝それに源泉1,021を足した額
    assert "仮受金（貸方）86,017 円" in texts
    assert "預り金 1,021 円" in texts
    assert "借方合計 87,038 円" in texts
