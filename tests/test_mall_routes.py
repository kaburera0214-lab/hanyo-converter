# -*- coding: utf-8 -*-
"""
経路表（lib/mall_routes.py）と実装・画面文言の整合を検証する。

守りたいこと: 「仕様が変わったのに、画面の説明文だけ古いまま残る」を検知すること。
文言の間違いは普通のテストでは落ちないので、経路表を実装に突き合わせる形で見張る。
"""
import importlib
import io
import os
import re

import pytest

from lib import mall_routes
from lib.receiving import runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "pages", "21_📥_入荷登録.py")


def _resolve(symbol):
    """'lib.pkg.mod:func' → 実体（無ければ None）。"""
    module_name, _, attr = symbol.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return getattr(module, attr, None)


@pytest.mark.parametrize("route", mall_routes.api_routes(),
                         ids=[f"{r.mall}-{r.field}" for r in mall_routes.api_routes()])
def test_api_route_implementation_exists(route):
    """「APIで自動更新します」と画面に書く以上、その実装が実在すること。

    実装を消した／改名したのに文言がAPIのまま残る、を落とす。
    """
    assert route.api_symbol, f"{route.mall}の{route.label}: api_symbolが未設定"
    assert callable(_resolve(route.api_symbol)), \
        f"{route.mall}の{route.label}: {route.api_symbol} が見つからない。" \
        "実装を変えたなら lib/mall_routes.py の経路表も直すこと（画面文言はここから作られる）。"


@pytest.mark.parametrize("route", mall_routes.manual_routes(),
                         ids=[f"{r.mall}-{r.field}" for r in mall_routes.manual_routes()])
def test_manual_route_has_no_api_implementation(route):
    """手動と案内している経路に、API実装が生えていないこと。

    APIを実装したらここが落ちる。落ちたら実装を消すのではなく、経路表を
    mode=API に直す（＝画面の「CSVでアップしてください」も自動で消える）。
    """
    for symbol in route.forbidden_symbols:
        assert _resolve(symbol) is None, \
            f"{route.mall}の{route.label}: {symbol} が実装されているのに、" \
            "経路表はまだ手動のまま。lib/mall_routes.py を mode=API に更新すること。"


@pytest.mark.parametrize("route", mall_routes.manual_routes(),
                         ids=[f"{r.mall}-{r.field}" for r in mall_routes.manual_routes()])
def test_manual_route_states_reason_and_check_date(route):
    """手動が残る経路は「なぜ手動か」と「いつ確認したか」を必ず持つこと。

    理由と確認日が無いと、モール側がAPIを出したあとも誰も見直さない。
    """
    assert route.why, f"{route.mall}の{route.label}: 手動が残る理由が未記入"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", route.checked or ""), \
        f"{route.mall}の{route.label}: 最終確認日が未記入"


def test_page_does_not_hardcode_route_wording():
    """入荷登録の画面が、経路をベタ書きしていないこと。

    「APIで自動」「CSVでアップ」を画面側に直接書くと経路表と二重管理になり、
    片方だけ古くなる。文言は lib/mall_routes.py から作る。
    """
    source = io.open(PAGE, encoding="utf-8").read()
    body = source.split('"""', 2)[2]        # モジュールdocstringは対象外
    banned = ["API自動反映済み", "APIに項目指定更新が"]
    found = [word for word in banned if word in body]
    assert not found, \
        f"画面に経路の説明がベタ書きされている: {found}。lib/mall_routes.py から組み立てること。"


def test_page_docstring_matches_runner_steps():
    """docstringのステップ番号が runner の実際のステップ名と一致すること。

    ステップを増やすと番号がずれ、実行結果の表（runner側）と説明（docstring側）が
    食い違う。実際に ①②③ のまま ⑤ まで増えていた。
    """
    docstring = io.open(PAGE, encoding="utf-8").read().split('"""')[1]
    steps = [runner.STEP_NE_MAIN, runner.STEP_NE_PRICE, runner.STEP_RAKUTEN_DELIVERY,
             runner.STEP_RAKUTEN_PRICE, runner.STEP_YAHOO_PRICE]
    for step in steps:
        number = step[0]                    # ①〜⑤
        name = step[1:].strip()
        assert number in docstring, f"docstringにステップ{number}の説明が無い: {step}"
        head = name.split("（")[0].split("　")[0]
        assert head in docstring, f"docstringのステップ{number}の説明が実装と違う: {step}"
