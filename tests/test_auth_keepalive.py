# -*- coding: utf-8 -*-
"""
認可延命（lib/auth_keepalive.py）の回帰テスト。

このバッチが黙って失敗すると、気づくのは「使おうとして止まったとき」になる。
特に押さえたいのは次の3つ:
  1. 1つの接続先が失敗しても、他の接続先の延命は必ず実行される
  2. 失効（要再認可）と、バッチの不具合を取り違えない（通知先が違う）
  3. 未設定は「異常」にも「正常」にもせず、スキップとして残す
"""
import datetime
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from batch import st_shim  # noqa: E402
st_shim.install()

from lib import auth_keepalive as ak  # noqa: E402


class _AuthError(Exception):
    """テスト用の認証切れ例外。"""


def _provider(key="test", touch=None, configured=True, label="テスト接続先"):
    return {
        "key": key,
        "label": label,
        "state_name": f"{key}_keepalive.json",
        "auth_error": _AuthError,
        "touch": touch or (lambda: {"rotated": True}),
        "is_configured": lambda: configured,
        "lifetime": "3日",
    }


@pytest.fixture(autouse=True)
def _no_drive(monkeypatch):
    """Driveへの読み書きはテストしないので、メモリ上のdictに差し替える。"""
    store = {}
    monkeypatch.setattr(ak, "load_state", lambda name: dict(store.get(name, {})))
    monkeypatch.setattr(ak, "save_state",
                        lambda name, state: store.__setitem__(name, dict(state)) or True)
    return store


# ---------------------------------------------------------------- 正常系

def test_成功するとトークン更新として記録される(_no_drive):
    r = ak.run_one(_provider(touch=lambda: {"rotated": True}))
    assert r["ok"] is True
    assert r["rotated"] is True
    assert _no_drive["test_keepalive.json"]["last_result"] == "ok"


def test_更新不要でも成功として扱う(_no_drive):
    r = ak.run_one(_provider(touch=lambda: {"rotated": False}))
    assert r["ok"] is True
    assert r["rotated"] is False
    assert "有効" in r["message"]


def test_未設定はスキップであって異常ではない(_no_drive):
    r = ak.run_one(_provider(configured=False))
    assert r["ok"] is True
    assert r["skipped"] is True
    # 黙って成功にせず、スキップしたことがメッセージに残る
    assert "スキップ" in r["message"]


# ---------------------------------------------------------------- 異常系

def test_失効は認証切れとして通知対象になる(_no_drive):
    def _expired():
        raise _AuthError("refresh token has expired.")

    r = ak.run_one(_provider(touch=_expired))
    assert r["ok"] is False
    assert r["auth"] is True          # ブラウザ再認可＝現場が直せる
    assert r["alert"] is True
    assert _no_drive["test_keepalive.json"]["last_result"] == "auth_error"


def test_想定外の例外は認証切れと区別される(_no_drive):
    def _boom():
        raise RuntimeError("Driveに繋がりません")

    r = ak.run_one(_provider(touch=_boom))
    assert r["ok"] is False
    assert r["auth"] is False         # 管理者向け。スタッフに再認可を頼んでも直らない
    assert r["alert"] is True
    assert _no_drive["test_keepalive.json"]["last_result"] == "error"


def test_モジュールを読めない接続先でも落ちない(_no_drive):
    r = ak.run_one({"key": "x", "label": "X", "broken": "No module named 'x'"})
    assert r["ok"] is False
    assert r["alert"] is True


# ---------------------------------------------------------------- 通知の間引き

def test_失効通知はクールダウン中は再送しない():
    today = datetime.date(2026, 8, 31)
    assert ak.should_alert({}, today) is True
    assert ak.should_alert({"last_alert_date": "2026-08-30"}, today) is False
    assert ak.should_alert({"last_alert_date": "2026-08-28"}, today) is True
    # 壊れた日付は「通知する」側に倒す（黙らせない）
    assert ak.should_alert({"last_alert_date": "こわれた"}, today) is True


def test_復旧すると次の失効で必ず通知できる(_no_drive):
    _no_drive["test_keepalive.json"] = {"last_alert_date": "2026-08-31"}
    ak.run_one(_provider(touch=lambda: {"rotated": True}))
    # 成功時に last_alert_date を消しているので、次に失効したら即通知される
    assert "last_alert_date" not in _no_drive["test_keepalive.json"]


# ---------------------------------------------------------------- 全体実行

def test_1つ失敗しても他の接続先は必ず実行される(monkeypatch, _no_drive):
    called = []

    def _ng():
        called.append("ng")
        raise _AuthError("expired")

    def _ok():
        called.append("ok")
        return {"rotated": True}

    monkeypatch.setattr(ak, "providers", lambda keys=None: [
        _provider(key="a", touch=_ng, label="A"),
        _provider(key="b", touch=_ok, label="B"),
    ])

    results = ak.run_all()
    assert called == ["ng", "ok"]     # 先が落ちても後が走る
    s = ak.summarize(results)
    assert s == {"total": 2, "ok": 1, "skipped": 0, "auth_error": 1, "error": 0}


def test_再認可の依頼先が接続先ごとに正しい():
    """Yahooの再認可には店舗オーナーのYahoo IDが要る。倉庫スタッフは持って
    いないので、現場に投げると「頼まれたのに動けない」状態になる。"""
    by_key = {d["key"]: d for d in ak.providers()}
    assert by_key["ne"]["reauth_audience"] == "staff"     # NEのIDは現場が持っている
    assert by_key["yahoo"]["reauth_audience"] == "admin"  # 店舗オーナーIDが要る


def test_失効結果に依頼先が載る(_no_drive):
    def _expired():
        raise _AuthError("expired")

    prov = _provider(touch=_expired)
    prov["reauth_audience"] = "admin"
    r = ak.run_one(prov)
    assert r["reauth_audience"] == "admin"


def test_実際の接続先定義が壊れていない():
    """NE・Yahooの定義が組み立てられること（importミスの検出）。"""
    defs = ak.providers()
    keys = {d["key"] for d in defs}
    assert keys == {"ne", "yahoo"}
    for d in defs:
        assert not d.get("broken"), f"{d['key']}: {d.get('broken')}"
        assert callable(d["touch"])
        assert issubclass(d["auth_error"], Exception)


def test_Yahooのkeep_aliveは強制リフレッシュする(monkeypatch):
    """access_token()は期限が近いときしか更新しないので、延命には使えない。
    keep_alive()は必ず_refreshを呼ぶこと。"""
    from lib.yahoo_api import client

    calls = []
    monkeypatch.setattr(client, "_load_tokens",
                        lambda: {"access_token": "a", "refresh_token": "r0",
                                 "expires_at": "2099-01-01T00:00:00"})  # 期限は遠い

    def _fake_refresh(rt):
        calls.append(rt)
        return {"access_token": "a2", "refresh_token": "r1",
                "saved_at": "2026-08-31T18:00:00", "expires_at": "2026-08-31T19:00:00"}

    monkeypatch.setattr(client, "_refresh", _fake_refresh)
    info = client.keep_alive()
    assert calls == ["r0"]            # 期限が遠くても必ず更新した
    assert info["rotated"] is True    # リフレッシュトークンが入れ替わった


def test_Yahoo未認可はYahooAuthErrorになる(monkeypatch):
    from lib.yahoo_api import client
    monkeypatch.setattr(client, "_load_tokens", lambda: None)
    with pytest.raises(client.YahooAuthError):
        client.keep_alive()


def test_Yahooのリフレッシュトークンが無ければ再認可を促す(monkeypatch):
    from lib.yahoo_api import client
    monkeypatch.setattr(client, "_load_tokens", lambda: {"access_token": "a"})
    with pytest.raises(client.YahooAuthError):
        client.keep_alive()


# ---------------------------------------------------------------- 通知文面

def test_接続先ごとに正しい再認可手順が出る():
    from lib.notify import auth_alerts

    ne = auth_alerts.reauth_body("ne", "https://example.streamlit.app")
    ya = auth_alerts.reauth_body("yahoo", "https://example.streamlit.app")

    assert "ネクストエンジン" in ne
    assert "NE API接続" in ne

    assert "Yahoo API接続" in ya
    assert "店舗オーナーのYahoo ID" in ya
    # 画面を移るとCSVが消える件を必ず案内する（案内どおりにやって詰まないように）
    assert "もう一度同じCSVをアップロードし直してください" in ya

    # 専門用語を現場向け文面に出さない
    for body in (ne, ya):
        for word in ("トークン", "GitHub", "refresh"):
            assert word not in body, f"現場向け文面に専門用語が出ています: {word}"


def test_未知の接続先でも文面生成で落ちない():
    from lib.notify import auth_alerts
    body = auth_alerts.reauth_body("unknown-provider")
    assert "unknown-provider" in body
