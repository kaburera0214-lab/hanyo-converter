# -*- coding: utf-8 -*-
"""
NE商品マスタ照会のAPI呼び出し回数（lib/ne_api/goods）の回帰テスト。

NE APIは呼び出し回数で課金される（無料枠1000回/月）。取扱数の多い取引先の価格改定で
無料枠を使い切らないよう、存在確認は一括（goods_id-in）で行う。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ne_api import client, goods  # noqa: E402


@pytest.fixture
def ne(monkeypatch):
    """NEに存在するコード集合を決めて client.call を差し替える。呼び出しを記録する。"""
    state = {"exists": set(), "calls": []}

    def fake_call(endpoint, params=None, **kw):
        state["calls"].append((endpoint, params or {}))
        p = params or {}
        if "goods_id-in" in p:
            wanted = {c.strip().lower() for c in p["goods_id-in"].split(",")}
            return {"data": [{"goods_id": g} for g in state["exists"]
                             if g.lower() in wanted]}
        if "goods_id-eq" in p:
            v = p["goods_id-eq"]
            return {"data": [{"goods_id": v}] if v in state["exists"] else []}
        if "goods_id-like" in p:
            v = p["goods_id-like"].lower()
            return {"data": [{"goods_id": g} for g in state["exists"] if v in g.lower()]}
        return {"data": []}

    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(goods.client, "call", fake_call)
    return state


def test_1500_items_uses_a_handful_of_calls(ne):
    """実運用の想定: 取扱数1500商品の取引先。旧実装は1500〜4500回で無料枠を即超過した。"""
    codes = [f"artc{i:05d}" for i in range(1500)]
    ne["exists"] = set(codes)

    found = goods.find_existing(codes)

    assert len(found) == 1500                       # 全件ちゃんと見つかる
    assert len(ne["calls"]) == 3                    # 500件ずつ＝3回だけ
    assert all("goods_id-in" in p for _, p in ne["calls"])
    assert goods.call_estimate(1500) < 10           # 画面の見積りも同じオーダー


def test_batch_result_maps_to_nes_exact_code(ne):
    """NE側の大文字小文字が入力と違っても、NEの正確なコードへ寄せて返す。"""
    ne["exists"] = {"ARTC6366"}
    found = goods.find_existing(["artc6366"])
    assert found == {"artc6366": "ARTC6366"}
    assert len(ne["calls"]) == 1


def test_missing_codes_fall_back_to_individual_lookup(ne):
    """一括で拾えなかったぶんだけ1件ずつ救済する（全体を1件ずつには戻さない）。"""
    ne["exists"] = {"artc0001", "artc0002"}
    found = goods.find_existing(["artc0001", "artc0002", "nothere"])

    assert found == {"artc0001": "artc0001", "artc0002": "artc0002"}
    ins = [p for _, p in ne["calls"] if "goods_id-in" in p]
    singles = [p for _, p in ne["calls"] if "goods_id-in" not in p]
    assert len(ins) == 1                            # 一括は1回
    # 個別照会は未ヒットの "nothere" だけ（大文字違いの試行を含む）。既存2件は引き直さない
    assert singles and all("nothere" in str(list(p.values())).lower() for p in singles)


def test_batch_failure_falls_back_without_losing_items(monkeypatch, ne):
    """一括検索が使えない環境でも取りこぼさない（従来どおり1件ずつで拾う）。"""
    ne["exists"] = {"artc0001"}
    real = client.call

    def flaky(endpoint, params=None, **kw):
        if params and "goods_id-in" in params:
            raise RuntimeError("この環境では -in が使えない")
        return real(endpoint, params, **kw)

    monkeypatch.setattr(goods.client, "call", flaky)
    assert goods.find_existing(["artc0001"]) == {"artc0001": "artc0001"}


def test_auth_error_is_not_swallowed(monkeypatch, ne):
    """認証切れは握り潰さない（再認可を促す必要がある）。"""
    def boom(endpoint, params=None, **kw):
        raise client.NEAuthError("未認可です")

    monkeypatch.setattr(goods.client, "call", boom)
    with pytest.raises(client.NEAuthError):
        goods.find_existing(["artc0001"])


def test_wait_policy_scales_with_rows():
    """大量アップは待ち時間を延ばし、ポーリング間隔を広げる（ポーリングも課金対象）。"""
    assert goods.wait_policy(10) == (120, 5)        # 少量は従来どおり
    assert goods.wait_policy(100) == (300, 10)
    assert goods.wait_policy(1500) == (900, 20)
    # 件数が増えても待ち回数（=API回数）は増え過ぎない
    assert 900 / 20 < 120 / 5 * 3


def test_call_estimate():
    assert goods.call_estimate(0) == 0
    assert goods.call_estimate(19) == 1 + 1 + 2     # 一括1回＋upload＋完了待ち
    assert goods.call_estimate(1500) == 3 + 1 + 3
