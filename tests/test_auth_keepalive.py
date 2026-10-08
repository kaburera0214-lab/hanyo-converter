# -*- coding: utf-8 -*-
"""
認可延命（lib/auth_keepalive.py）の回帰テスト。

このバッチが黙って失敗すると、気づくのは「使おうとして止まったとき」になる。
特に押さえたいのは次の5つ:
  1. 1つの接続先が失敗しても、他の接続先の延命は必ず実行される
  2. 失効（要再認可）と、バッチの不具合を取り違えない（通知先が違う）
  3. 必須の接続先が未設定なら失敗にする（スキップを成功に数えない）
  4. 延ばせない接続先（Yahoo）は、期限の手前で再認可を依頼し、間際は失敗にする
  5. 期限が分からないときに「正常」にしない
"""
import datetime
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from batch import st_shim  # noqa: E402

# シムは sys.modules["streamlit"] を丸ごと置き換えるので、このモジュールを読み込んだ
# だけで後続のテストにも漏れる（本物のstreamlitを使うテストが軒並み壊れる）。
# 元のモジュールを控えておき、このファイルのテストが終わったら必ず戻す。
_REAL_STREAMLIT = sys.modules.get("streamlit")
st_shim.install()

from lib import auth_keepalive as ak  # noqa: E402


def teardown_module(module):  # noqa: ARG001
    """シムを後片付けする。入れっぱなしにしない。"""
    if _REAL_STREAMLIT is not None:
        sys.modules["streamlit"] = _REAL_STREAMLIT
    else:
        sys.modules.pop("streamlit", None)


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


def test_必須の接続先が未設定なら失敗にする(_no_drive):
    """2026-08-31〜10-07: Yahooが未設定スキップ＝成功になり、一度も見ていなかった。"""
    prov = _provider(configured=False)
    prov["required"] = True
    r = ak.run_one(prov)
    assert r["ok"] is False
    assert not r.get("skipped")
    assert r["alert"] is True
    assert ak.summarize([r])["failed"] == 1


def test_実際の接続先はどちらも必須():
    for d in ak.providers():
        assert d.get("required") is True, f"{d['key']} が必須になっていません"


def test_必須でない接続先の未設定はスキップとして残す(_no_drive):
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
    assert s == {"total": 2, "ok": 1, "skipped": 0, "auth_error": 1,
                 "expiring": 0, "error": 0, "failed": 1}


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
                        lambda force=False: {"access_token": "a", "refresh_token": "r0",
                                             "expires_at": "2099-01-01T00:00:00"})  # 期限は遠い

    def _fake_refresh(rt, authorized=""):
        calls.append(rt)
        return {"access_token": "a2", "refresh_token": "r1",
                "saved_at": "2026-08-31T18:00:00", "expires_at": "2026-08-31T19:00:00"}

    monkeypatch.setattr(client, "_refresh", _fake_refresh)
    info = client.keep_alive()
    assert calls == ["r0"]            # 期限が遠くても必ず更新した
    assert info["rotated"] is True    # リフレッシュトークンが入れ替わった


def test_Yahoo未認可はYahooAuthErrorになる(monkeypatch):
    from lib.yahoo_api import client
    monkeypatch.setattr(client, "_load_tokens", lambda force=False: None)
    with pytest.raises(client.YahooAuthError):
        client.keep_alive()


def test_Yahooのリフレッシュトークンが無ければ再認可を促す(monkeypatch):
    from lib.yahoo_api import client
    monkeypatch.setattr(client, "_load_tokens", lambda force=False: {"access_token": "a"})
    with pytest.raises(client.YahooAuthError):
        client.keep_alive()


# ---------------------------------------------------------------- 延ばせない接続先（Yahoo）

def _expiring_provider(days_left, text="次の再認可の期限: 2026-11-04（あとN日）"):
    prov = _provider(touch=lambda: {"rotated": False, "days_left": days_left,
                                    "deadline_text": text})
    prov.update({"warn_days": 7, "fail_days": 3, "reauth_audience": "admin"})
    return prov


def test_期限まで余裕があれば正常で通知もしない(_no_drive):
    r = ak.run_one(_expiring_provider(20))
    assert r["ok"] is True and not r.get("expiring") and not r.get("alert")
    assert _no_drive["test_keepalive.json"]["days_left"] == 20


def test_期限の7日前から再認可を依頼するがまだ失敗にはしない(_no_drive):
    r = ak.run_one(_expiring_provider(7))
    assert r["ok"] is True                # まだ動いているのでワークフローは緑のまま
    assert r["expiring"] is True and r["alert"] is True
    assert r["reauth_audience"] == "admin"
    assert "延長はできない" in r["message"]


def test_期限の3日前を切ったら失敗にして監視を赤にする(_no_drive):
    r = ak.run_one(_expiring_provider(3))
    assert r["ok"] is False and r["expiring"] is True
    s = ak.summarize([r])
    assert s["failed"] == 1 and s["expiring"] == 1
    assert s["auth_error"] == 0 and s["error"] == 0    # 失効でも不具合でもない


def test_期限間近の依頼は3日おきで毎回は送らない(_no_drive):
    now = datetime.datetime(2026, 10, 28, 5, 0)
    first = ak.run_one(_expiring_provider(7), now=now)
    same_day = ak.run_one(_expiring_provider(7), now=now.replace(hour=17))
    later = ak.run_one(_expiring_provider(4), now=now + datetime.timedelta(days=3))
    assert [first["alert"], same_day["alert"], later["alert"]] == [True, False, True]


def test_期限が分からないときは正常にしない(_no_drive):
    r = ak.run_one(_expiring_provider(None, text="再認可の期限を判定できません"))
    assert r["ok"] is False and r["alert"] is True
    assert not r.get("expiring")          # 期限間近ではなく「見張れていない」＝不具合扱い
    assert ak.summarize([r])["error"] == 1


def test_Yahooの期限は認可した日から28日で更新しても延びない(monkeypatch):
    from lib.yahoo_api import client
    saved = {}
    monkeypatch.setattr(client, "_basic_header", lambda: {})
    monkeypatch.setattr(client, "_parse", lambda res: {"access_token": "a2", "expires_in": 3600})
    monkeypatch.setattr(client.requests, "post", lambda *a, **k: object())

    def _fake_save(access, refresh, expires_in, authorized_at=""):
        saved.update({"refresh_token": refresh, "authorized_at": authorized_at})
        return dict(saved)

    monkeypatch.setattr(client, "_save_tokens", _fake_save)
    monkeypatch.setattr(client, "_load_tokens", lambda force=False: {
        "access_token": "a", "refresh_token": "r0", "authorized_at": "2026-10-07T05:43:05"})

    info = client.keep_alive()
    assert saved["refresh_token"] == "r0"                    # 新しいトークンは返らない
    assert saved["authorized_at"] == "2026-10-07T05:43:05"   # 認可日時は更新で動かさない
    assert info["rotated"] is False

    tokens = {"authorized_at": "2026-10-07T05:43:05"}
    assert client.days_until_reauth(tokens, datetime.datetime(2026, 10, 7, 6, 0)) == 27
    assert client.days_until_reauth(tokens, datetime.datetime(2026, 10, 28, 6, 0)) == 6
    assert client.days_until_reauth(tokens, datetime.datetime(2026, 11, 4, 6, 0)) == -1
    assert "2026-11-04" in client.deadline_text(tokens, datetime.datetime(2026, 10, 28, 6, 0))
    # 壊れた日時は「判定できない」。0日や無期限に丸めない
    assert client.days_until_reauth({"authorized_at": "こわれた"}) is None
    assert "判定できません" in client.deadline_text({"authorized_at": "こわれた"})


def test_画面が覚えている古いトークンで失敗したらDriveを読み直す(monkeypatch):
    """別の画面で再認可された後も、古いトークンを使い続けて同じ認証切れを出していた。"""
    from lib.yahoo_api import client
    used = []

    def _fake_refresh(rt, authorized=""):
        used.append(rt)
        if rt == "old":
            raise client.YahooAuthError("refresh token has expired.")
        return {"access_token": "new-access", "refresh_token": rt}

    stale = {"access_token": "a", "refresh_token": "old", "expires_at": "2000-01-01T00:00:00"}
    fresh = {"access_token": "b", "refresh_token": "new", "expires_at": "2000-01-01T00:00:00"}
    monkeypatch.setattr(client, "_refresh", _fake_refresh)
    monkeypatch.setattr(client, "_load_tokens", lambda force=False: fresh if force else stale)
    assert client.access_token() == "new-access"
    assert used == ["old", "new"]

    # Drive側も同じトークンなら、本当に切れている＝そのまま認証切れとして出す
    used.clear()
    monkeypatch.setattr(client, "_load_tokens", lambda force=False: stale)
    with pytest.raises(client.YahooAuthError):
        client.access_token()
    assert used == ["old"]


# ---------------------------------------------------------------- 通知文面

def test_接続先ごとに正しい再認可手順が出る():
    from lib.notify import auth_alerts

    ne = auth_alerts.reauth_body("ne", "https://example.streamlit.app")
    ya = auth_alerts.reauth_body("yahoo", "https://example.streamlit.app")

    assert "ネクストエンジン" in ne
    assert "NE API接続" in ne

    assert "Yahoo API接続" in ya
    assert "店舗オーナーのYahoo ID" in ya
    # 部分反映後に押し直すと価格が二重に上がる。案内どおりにやって
    # 間違った金額を付けさせないよう、明確に止める文面であること。
    assert "アップし直して「確定して反映」を押さないでください" in ya
    assert "犬飼に連絡してください" in ya

    # 専門用語を現場向け文面に出さない
    for body in (ne, ya):
        for word in ("トークン", "GitHub", "refresh"):
            assert word not in body, f"現場向け文面に専門用語が出ています: {word}"


def test_未知の接続先でも文面生成で落ちない():
    from lib.notify import auth_alerts
    body = auth_alerts.reauth_body("unknown-provider")
    assert "unknown-provider" in body
    assert "unknown-provider" in auth_alerts.expiring_body("unknown-provider", "あと3日")


def test_切れる前の依頼は期限と手順が入りまだ動いていると分かる():
    from lib.notify import auth_alerts
    body = auth_alerts.expiring_body("yahoo", "次の再認可の期限: 2026-11-04（あと6日）",
                                     "https://example.streamlit.app")
    assert "2026-11-04（あと6日）" in body
    assert "いまはまだ動いています" in body
    assert "Yahooにログインして認可する" in body
    assert "店舗オーナーのYahoo ID" in body
    for word in ("トークン", "GitHub", "refresh"):
        assert word not in body


def test_切れた後の依頼は控えからの再実行を案内する():
    from lib.notify import auth_alerts
    body = auth_alerts.reauth_body("yahoo", "https://example.streamlit.app")
    assert "反映できていない処理" in body and "再実行" in body


# ---------------------------------------------------------------- 期限の控え（業務デスクが読む）

def _deadline_result(key="yahoo", authorized="2026-10-07T05:43:05+00:00",
                     deadline="2026-11-04T05:43:05+00:00"):
    return {"key": key, "label": "Yahoo", "ok": True, "authorized_at": authorized,
            "deadline": deadline, "warn_days": 7, "fail_days": 3}


def test_期限の控えは再認可したときだけ中身が変わる():
    first = ak.deadline_status([_deadline_result()])
    assert first["connections"]["yahoo"]["deadline"] == "2026-11-04T05:43:05+00:00"
    assert first["connections"]["yahoo"]["warn_days"] == 7
    # 同じ認可のまま何度実行しても同じ（確認時刻や残り日数を入れていない＝コミットが増えない）
    assert ak.deadline_status([_deadline_result()], first) == first
    again = ak.deadline_status(
        [_deadline_result(authorized="2026-10-30T01:00:00+00:00",
                          deadline="2026-11-27T01:00:00+00:00")], first)
    assert again != first
    assert again["connections"]["yahoo"]["deadline"] == "2026-11-27T01:00:00+00:00"


def test_期限を読めなかった回は前回の控えを消さない():
    """認証切れや不具合で期限が取れない回に控えを空にすると、見張りごと消える。"""
    first = ak.deadline_status([_deadline_result()])
    broken = [{"key": "yahoo", "label": "Yahoo", "ok": False, "auth": True}]
    assert ak.deadline_status(broken, first) == first
    # 期限の無い接続先（NE）は載らない
    assert "ne" not in ak.deadline_status(
        [{"key": "ne", "label": "ネクストエンジン", "ok": True}], first)["connections"]


def test_延ばせない接続先の結果に認可日時と期限が載る(_no_drive):
    prov = _provider(touch=lambda: {
        "rotated": False, "days_left": 20, "deadline_text": "x",
        "authorized_at": "2026-10-07T05:43:05+00:00",
        "deadline": "2026-11-04T05:43:05+00:00"})
    prov.update({"warn_days": 7, "fail_days": 3})
    r = ak.run_one(prov)
    status = ak.deadline_status([r])
    assert status["connections"]["test"]["authorized_at"] == "2026-10-07T05:43:05+00:00"
    assert status["connections"]["test"]["fail_days"] == 3
