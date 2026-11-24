# -*- coding: utf-8 -*-
"""
便種・個口の変更（pages/24）：倉庫のPCの担当が止まった・起き直したときの画面（2026-11-24）。

担当は止まると1分後に起き直すが、止まっていた間の依頼は拾わない。画面が「もう一度押して」と出し、
起き直せないとき（上限・PCが落ちている）は管理者のChatworkへ直接知らせる。
"""
import datetime
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_page12_soufurikomi_render import _app_test  # noqa: E402

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "pages", "24_📦_便種・個口の変更.py")
JST = datetime.timezone(datetime.timedelta(hours=9))


@pytest.fixture
def setup(monkeypatch):
    if not (8 <= datetime.datetime.now(JST).hour < 20):
        pytest.skip("受付時間（8:00〜20:00 JST）の外では、時間外の案内が出るので見られない")
    from cryptography.fernet import Fernet
    import requests
    from lib import auth
    from lib.notify import chatwork

    key = Fernet.generate_key()
    box = {"state": None, "told": []}

    class Res:
        def raise_for_status(self):
            pass

        def json(self):
            if box["state"] is None:
                return []
            return [{"ciphertext": Fernet(key).encrypt(json.dumps(box["state"]).encode()).decode()}]

    # 自動更新の部品はこのPCに入っていないことがある（画面の判定には関係しない）
    import types
    monkeypatch.setitem(sys.modules, "streamlit_autorefresh",
                        types.SimpleNamespace(st_autorefresh=lambda **k: None))
    monkeypatch.setattr(auth, "require_role", lambda *a, **k: None)
    monkeypatch.setattr(requests, "post", lambda *a, **k: Res())
    monkeypatch.setattr(chatwork, "create_task", lambda body, **k: box["told"].append(body) or True)
    at = _app_test().from_file(PAGE, default_timeout=30)
    at.secrets["SHARED_SECRETS_URL"] = "https://example.invalid"
    at.secrets["SHARED_SECRETS_ANON_KEY"] = "a"
    at.secrets["SHARED_SECRETS_KEY"] = key.decode()
    return at, box


def _req(sent_ts):
    return {"req_id": "abc123", "order_id": "100001", "delivery_id": "28", "boxes": 2, "sent_ts": sent_ts}


def _texts(at):
    return " ".join(e.value for e in list(at.warning) + list(at.error) + list(at.info))


def test_起き直した後は押し直しを案内する(setup):
    at, box = setup
    now = time.time()
    box["state"] = {"requests": {}, "started_ts": now, "published_ts": now}
    at.session_state["chg_req"] = _req(now - 120)
    at.run()
    assert not at.exception, at.exception
    assert "起き直しました。この依頼は届いていません" in _texts(at)
    assert "確かめています" not in _texts(at)
    assert box["told"] == []


def test_実行の途中で起き直したら確かめ直しを案内する(setup):
    at, box = setup
    now = time.time()
    box["state"] = {"requests": {}, "started_ts": now, "published_ts": now}
    at.session_state["chg_req"] = _req(now - 120)
    at.session_state["chg_ran"] = "abc123"
    at.run()
    assert "実行の途中で起き直しました" in _texts(at)


def test_担当が動いていなければ管理者に1回だけ知らせる(setup):
    at, box = setup
    old = time.time() - 900
    box["state"] = {"requests": {"abc123": {"status": "checking"}}, "started_ts": old, "published_ts": old}
    at.session_state["chg_req"] = _req(old + 10)
    at.run()
    at.run()
    assert not at.exception, at.exception
    assert "管理者にChatworkで知らせました" in _texts(at)
    assert len(box["told"]) == 1


def test_起こし直しの上限なら止めていると出す(setup):
    at, box = setup
    now = time.time()
    box["state"] = {"requests": {}, "gave_up": True, "started_ts": now, "published_ts": now}
    at.session_state["chg_req"] = _req(now - 60)
    at.run()
    assert "何度も止まったため、止めています" in _texts(at)
    assert "起き直しました" not in _texts(at)


def test_ふだんは何も出さない(setup):
    at, box = setup
    now = time.time()
    box["state"] = {"requests": {"abc123": {"status": "checking"}}, "started_ts": now - 3600, "published_ts": now}
    at.session_state["chg_req"] = _req(now - 30)
    at.run()
    assert "確かめています" in _texts(at)
    assert "起き直し" not in _texts(at) and box["told"] == []
