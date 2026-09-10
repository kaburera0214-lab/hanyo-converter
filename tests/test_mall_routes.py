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
             runner.STEP_RAKUTEN_PRICE, runner.STEP_YAHOO_PRICE, runner.STEP_YAHOO_DELIVERY]
    for step in steps:
        number = step[0]                    # ①〜⑤
        name = step[1:].strip()
        assert number in docstring, f"docstringにステップ{number}の説明が無い: {step}"
        head = name.split("（")[0].split("　")[0]
        assert head in docstring, f"docstringのステップ{number}の説明が実装と違う: {step}"


def test_seed_yahoo_group_no_fills_only_missing():
    """配送グループNoは自動投入するが、保存済みの値は絶対に上書きしない。

    現場に手入力させると打ち間違いがそのまま送料設定になるので自動で入れる。
    一方、店舗側でグループを組み替えて画面から直した値をアプリが戻すと、
    直したはずが元に戻るという最悪の壊れ方をするので、既存値には触らない。
    """
    empty = {}
    seeded = mall_routes.seed_yahoo_group_no(empty)
    assert seeded["yahoo_group_takuhai"] == "1" and seeded["yahoo_group_mail"] == "2"
    assert seeded["yahoo_group_bins"]["3"] == "宅配便"     # No→便種の対応表も入る
    assert empty["yahoo_group_takuhai"] == "1" and empty["yahoo_group_mail"] == "2"

    edited = {"yahoo_group_takuhai": "6", "yahoo_group_mail": "5",
              "yahoo_group_bins": {"6": "宅配便"}}
    assert mall_routes.seed_yahoo_group_no(edited) == {}
    assert edited["yahoo_group_takuhai"] == "6" and edited["yahoo_group_bins"] == {"6": "宅配便"}

    partial = {"yahoo_group_takuhai": "3", "yahoo_group_bins": {"3": "宅配便"}}
    assert mall_routes.seed_yahoo_group_no(partial) == {"yahoo_group_mail": "2"}
    assert partial["yahoo_group_takuhai"] == "3"


# ══ 使ってはいけないAPI ══════════════════════════════════════

FORBIDDEN_YAHOO_APIS = {
    "editItem": "省略した項目をデフォルト値で上書きする（商品名・価格・カテゴリ・"
                "説明文などが初期値に戻る）。部分更新には絶対に使わない。",
    "uploadItemFile type=2": "「上書き（全入れ替え）」＝ストアの商品を全消去して"
                             "CSVと入れ替える。絶対に使わない。",
}


def test_edit_item_api_is_never_called():
    """editItem を呼ぶコードが1行も無いこと（2026-09-10 ユーザー決定・恒久禁止）。

    配送グループのようにAPIで部分更新したくなったときの誘惑がここにある。
    editItemは「商品登録API」という名前で部分更新に見えるが、送らなかった項目を
    デフォルト値で上書きするので、1回呼ぶだけで商品が壊れる。
    項目指定で更新したいときは uploadItemFile(type=4) を使う。
    """
    import pathlib

    root = pathlib.Path(ROOT)
    hits = []
    globs = ["lib/**/*.py", "pages/*.py", "batch/*.py", "tools/*.py"]
    for pattern in globs:
        for path in root.glob(pattern):
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "editItem" not in line:
                    continue
                if line.lstrip().startswith("#") or "🚫" in line:
                    continue          # 禁止理由を書いたコメントは可
                hits.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not hits, (
        "editItem を使っているコードがあります。禁止です（"
        + FORBIDDEN_YAHOO_APIS["editItem"] + "）: " + " / ".join(hits))


def test_upload_type_is_field_specified_only():
    """商品アップロードAPIに渡す type が「項目指定」だけであること。

    type=2（上書き）はストアの商品を全消去して入れ替える。事故ると店が消える。
    """
    from lib.yahoo_api import item_upload

    assert item_upload.TYPE_FIELD_SPECIFIED == 4
    source = io.open(os.path.join(ROOT, "lib", "yahoo_api", "item_upload.py"),
                     encoding="utf-8").read()
    body = source.split('"""', 2)[2]
    assert '"type": str(TYPE_FIELD_SPECIFIED)' in body, \
        "typeは定数TYPE_FIELD_SPECIFIED（=4 項目指定）以外を渡さないこと"
