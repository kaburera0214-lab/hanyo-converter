# -*- coding: utf-8 -*-
"""
価格改定のAPI直更新（lib/pricing/apply）の回帰テスト。

本番APIは叩かず、apply が呼ぶ関数（goods.upload_goods / rakuten_price.set_price /
yahoo items.update_prices）を差し替えて「何を渡したか」「失敗をどう記録するか」を確かめる。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.event import rms_api                       # noqa: E402
from lib.ne_api import goods                        # noqa: E402
from lib.pricing import apply, export as ex, rakuten_price  # noqa: E402

# kei0018 は枝番なし＝単品SKU（SKU対応表に無くても出力できる）。
# artc0486-01 は枝番付きなのでSKU対応表が要る。
SKU_TABLE = {"artc0486-01": ("artc0486", "artc0486-01", "1234")}


def _result_df():
    """結果表（画面の表と同じ形）: 変更あり2件＋据え置き1件＋計算不可1件。"""
    return pd.DataFrame([
        {"商品コード": "kei0018", "現販売価格": 4000, "新販売価格": 4500,
         "NE売価": 4091, "新下代": 2000},
        {"商品コード": "artc0486-01", "現販売価格": 891, "新販売価格": 901,
         "NE売価": 819, "新下代": 365},
        {"商品コード": "keep0001", "現販売価格": 1200, "新販売価格": 1200,
         "NE売価": 1091, "新下代": 500},
        {"商品コード": "ng0001", "現販売価格": 0, "新販売価格": None,
         "NE売価": None, "新下代": 300},
    ])


def test_split_targets_matches_csv_rule():
    """NEは計算できた全行（原価更新のため据え置きも）／モールは価格が変わる行だけ。"""
    ok, changed = apply.split_targets(_result_df(), include_unchanged=False)
    assert list(ok["商品コード"]) == ["kei0018", "artc0486-01", "keep0001"]
    assert list(changed["商品コード"]) == ["kei0018", "artc0486-01"]

    _, changed_all = apply.split_targets(_result_df(), include_unchanged=True)
    assert list(changed_all["商品コード"]) == ["kei0018", "artc0486-01", "keep0001"]


def test_build_tasks():
    tasks, notes = apply.build_tasks(_result_df(), SKU_TABLE)

    # NE: 計算不可の行は入らない。売価は税抜(int)、原価は新下代。
    assert tasks["ne_price"] == [
        {"syohin_code": "kei0018", "baika_tnk": 4091, "genka_tnk": 2000},
        {"syohin_code": "artc0486-01", "baika_tnk": 819, "genka_tnk": 365},
        {"syohin_code": "keep0001", "baika_tnk": 1091, "genka_tnk": 500},
    ]
    assert notes["ne_skipped"] == []

    # 楽天: 商品管理番号ごとにSKU価格をまとめる（単品SKUはコード自身がSKU管理番号）
    assert tasks["rakuten_price"] == [
        {"商品管理番号": "kei0018", "sku_prices": {"kei0018": 4500},
         "対象コード": ["kei0018"]},
        {"商品管理番号": "artc0486", "sku_prices": {"artc0486-01": 901},
         "対象コード": ["artc0486-01"]},
    ]
    assert notes["rakuten_missing"] == []

    # Yahoo: 親コード単位
    assert tasks["yahoo_price"] == {"kei0018": 4500, "artc0486": 901}
    assert apply.task_counts(tasks) == {"ne": 3, "rakuten": 2, "rakuten_sku": 2, "yahoo": 2}


def test_build_tasks_matches_csv_output():
    """API直更新とCSVが同じ商品・同じ価格になる（片方だけ反映されるのを防ぐ）。"""
    df = _result_df()
    tasks, _ = apply.build_tasks(df, SKU_TABLE)
    _, changed = apply.split_targets(df, include_unchanged=False)
    records, _ = ex.rakuten_rows(apply.mall_rows_of(changed), SKU_TABLE)

    from_csv = {r["SKU管理番号"]: r["販売価格"] for r in records if r["SKU管理番号"]}
    from_api = {sku: price for t in tasks["rakuten_price"]
                for sku, price in t["sku_prices"].items()}
    assert from_api == from_csv


def test_build_tasks_skips_missing_sku_and_empty_ne():
    df = pd.DataFrame([
        {"商品コード": "unknown-01", "現販売価格": 1000, "新販売価格": 1100,
         "NE売価": 1000, "新下代": 400},
        {"商品コード": "kei0018", "現販売価格": 4000, "新販売価格": 4500,
         "NE売価": None, "新下代": 2000},
    ])
    tasks, notes = apply.build_tasks(df, {})
    # 枝番付きでSKU対応表に無い → 楽天は対象外（CSVと同じ判定）
    assert notes["rakuten_missing"] == ["unknown-01"]
    assert [t["商品管理番号"] for t in tasks["rakuten_price"]] == ["kei0018"]
    # NE売価が空 → NEは空値を送れないので対象外
    assert notes["ne_skipped"] == ["kei0018"]
    assert [r["syohin_code"] for r in tasks["ne_price"]] == ["unknown-01"]


def test_build_tasks_systems_filter():
    tasks, _ = apply.build_tasks(_result_df(), SKU_TABLE, systems={"ne"})
    assert "ne_price" in tasks
    assert "rakuten_price" not in tasks and "yahoo_price" not in tasks


# ══ execute（APIは差し替えて検証） ══════════════════════════

@pytest.fixture
def stub(monkeypatch):
    """3システムのAPI呼び出しを記録するだけのスタブに差し替える。"""
    calls = {"ne": [], "rakuten": [], "yahoo": [], "publish": 0}

    monkeypatch.setattr(goods, "find_existing",
                        lambda codes: {str(c).lower(): str(c) for c in codes})
    monkeypatch.setattr(goods, "upload_goods",
                        lambda rows: calls["ne"].append(rows) or "QUE1")
    monkeypatch.setattr(goods, "wait_que", lambda que_id, **kw: (True, "完了"))
    monkeypatch.setattr(rakuten_price, "set_price",
                        lambda mn, sku_prices: calls["rakuten"].append((mn, sku_prices)))

    class _Items:
        @staticmethod
        def update_prices(price_by_code):
            calls["yahoo"].append(price_by_code)
            return len(price_by_code), []

        @staticmethod
        def reserve_publish():
            calls["publish"] += 1
            return []

    class _Client:
        @staticmethod
        def access_token():
            return "dummy"

    monkeypatch.setitem(sys.modules, "lib.yahoo_api.items", _Items)
    monkeypatch.setitem(sys.modules, "lib.yahoo_api.client", _Client)
    return calls


def test_execute_all_success(stub):
    tasks, _ = apply.build_tasks(_result_df(), SKU_TABLE)
    results, failed = apply.execute(tasks)

    assert failed == {}
    assert [r["状態"] for r in results] == ["成功"] * 4      # NE1 + 楽天2 + Yahoo1
    assert len(stub["ne"][0]) == 3                            # NEは1回のuploadでまとめて
    assert stub["rakuten"] == [("kei0018", {"kei0018": 4500}),
                               ("artc0486", {"artc0486-01": 901})]
    assert stub["yahoo"] == [{"kei0018": 4500, "artc0486": 901}]
    assert stub["publish"] == 1                               # 反映予約は1回だけ


def test_execute_ne_code_not_found(stub, monkeypatch):
    """NEに無い商品コードは「新規登録扱い」を避けて明確な失敗にし、再実行対象に残す。"""
    monkeypatch.setattr(goods, "find_existing",
                        lambda codes: {c.lower(): c for c in codes if c != "keep0001"})
    tasks, _ = apply.build_tasks(_result_df(), SKU_TABLE, systems={"ne"})
    results, failed = apply.execute(tasks)

    ng = [r for r in results if r["状態"] == "失敗"]
    assert len(ng) == 1 and ng[0]["対象"] == "keep0001"
    assert [r["syohin_code"] for r in failed["ne_price"]] == ["keep0001"]
    assert len(stub["ne"][0]) == 2                            # 残り2件はちゃんと送る


def test_execute_rakuten_auth_error_stops_rest(stub, monkeypatch):
    """認証切れは以降も必ず失敗するので、残りは叩かずスキップ記録して再実行対象に残す。"""
    def _boom(mn, sku_prices):
        raise rms_api.RMSAuthError("RMSの認証に失敗しました")

    monkeypatch.setattr(rakuten_price, "set_price", _boom)
    tasks, _ = apply.build_tasks(_result_df(), SKU_TABLE, systems={"rakuten"})
    results, failed = apply.execute(tasks)

    assert [r["状態"] for r in results] == ["失敗", "スキップ"]
    assert len(failed["rakuten_price"]) == 2
    assert apply.has_auth_error(results)


def test_execute_rakuten_partial_failure(stub, monkeypatch):
    """1商品が失敗しても他は進める。失敗した分だけが再実行キューに入る。"""
    def _one_bad(mn, sku_prices):
        if mn == "artc0486":
            raise rms_api.RMSError("400 Bad Request")
        stub["rakuten"].append((mn, sku_prices))

    monkeypatch.setattr(rakuten_price, "set_price", _one_bad)
    tasks, _ = apply.build_tasks(_result_df(), SKU_TABLE, systems={"rakuten"})
    results, failed = apply.execute(tasks)

    assert [r["状態"] for r in results] == ["成功", "失敗"]
    assert [t["商品管理番号"] for t in failed["rakuten_price"]] == ["artc0486"]
    assert stub["rakuten"] == [("kei0018", {"kei0018": 4500})]


def test_execute_yahoo_error_is_retryable(stub, monkeypatch):
    """Yahooのエラーは失敗として残し、同じ形（dict）で再実行できるようにする。"""
    class _Bad:
        @staticmethod
        def update_prices(price_by_code):
            return 0, ["it-02022 sale_priceが指定されていません"]

        @staticmethod
        def reserve_publish():
            raise AssertionError("更新に失敗したら反映予約は呼ばない")

    monkeypatch.setitem(sys.modules, "lib.yahoo_api.items", _Bad)
    tasks, _ = apply.build_tasks(_result_df(), SKU_TABLE, systems={"yahoo"})
    results, failed = apply.execute(tasks)

    assert results[0]["状態"] == "失敗"
    assert failed["yahoo_price"] == tasks["yahoo_price"]
    assert apply.summarize(results) == (0, 1, 0)


def test_execute_retry_only_failed(stub, monkeypatch):
    """failed をそのまま execute に渡し直せる（再実行ボタンの動き）。"""
    calls = {"n": 0}

    def _flaky(mn, sku_prices):
        calls["n"] += 1
        if calls["n"] == 2:                       # 1回目の2商品目だけ失敗させる
            raise rms_api.RMSError("一時エラー")
        stub["rakuten"].append((mn, sku_prices))

    monkeypatch.setattr(rakuten_price, "set_price", _flaky)
    tasks, _ = apply.build_tasks(_result_df(), SKU_TABLE, systems={"rakuten"})
    _, failed = apply.execute(tasks)
    results2, failed2 = apply.execute(failed)     # 失敗した分だけ再実行

    assert failed2 == {}
    assert [r["状態"] for r in results2] == ["成功"]
    assert stub["rakuten"] == [("kei0018", {"kei0018": 4500}),
                               ("artc0486", {"artc0486-01": 901})]


def test_empty_tasks_do_nothing(stub):
    results, failed = apply.execute({})
    assert results == [] and failed == {}
    assert stub["ne"] == [] and stub["rakuten"] == [] and stub["yahoo"] == []
