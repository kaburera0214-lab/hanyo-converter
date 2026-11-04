# -*- coding: utf-8 -*-
"""ライセンスキーを共通の置き場から読む（lib/event/rms_api.py）のテスト。通信はしない。"""
import pytest
from cryptography.fernet import Fernet

from lib.event import rms_api

FKEY = Fernet.generate_key().decode()


class Resp:
    def __init__(self, rows, status=200):
        self._rows, self.status_code = rows, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._rows


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    rms_api._shared.update(at=0.0, value=None, ok=None)
    rms_api._source.update(source=None, detail="")
    for k in ("SHARED_SECRETS_URL", "SHARED_SECRETS_ANON_KEY", "SHARED_SECRETS_KEY",
              "RMS_LICENSE_KEY", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rms_api, "_plain_secret", lambda n: __import__("os").environ.get(n, ""))


def configure(monkeypatch, old="OLDKEY0000"):
    monkeypatch.setenv("SHARED_SECRETS_URL", "https://x.supabase.co")
    monkeypatch.setenv("SHARED_SECRETS_ANON_KEY", "anon")
    monkeypatch.setenv("SHARED_SECRETS_KEY", FKEY)
    monkeypatch.setenv("RMS_LICENSE_KEY", old)


def row(value):
    return [{"ciphertext": Fernet(FKEY.encode()).encrypt(value.encode()).decode(),
             "last4": value[-4:], "expiry": "2027-01-25"}]


def test_reads_the_latest_key_from_the_shared_store(monkeypatch):
    configure(monkeypatch)
    calls = []
    monkeypatch.setattr(rms_api.requests, "post",
                        lambda url, json, headers, timeout: calls.append(json) or Resp(row("NEWKEY6NUX")))
    assert rms_api._get_secret("RMS_LICENSE_KEY") == "NEWKEY6NUX"
    assert calls[0] == {"p_name": "rms_license_key", "p_reader": "streamlit"}
    assert rms_api.key_source()["source"] == "shared"


def test_github_actions_is_recorded_as_its_own_reader(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    calls = []
    monkeypatch.setattr(rms_api.requests, "post",
                        lambda url, json, headers, timeout: calls.append(json) or Resp(row("NEWKEY6NUX")))
    rms_api._get_secret("RMS_LICENSE_KEY")
    assert calls[0]["p_reader"] == "github-actions"


def test_reuses_for_ten_minutes(monkeypatch):
    configure(monkeypatch)
    calls = []
    monkeypatch.setattr(rms_api.requests, "post",
                        lambda *a, **k: calls.append(1) or Resp(row("NEWKEY6NUX")))
    rms_api._get_secret("RMS_LICENSE_KEY")
    rms_api._get_secret("RMS_LICENSE_KEY")
    assert len(calls) == 1


def test_falls_back_to_secrets_and_says_so(monkeypatch):
    configure(monkeypatch, old="OLDKEY0000")

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(rms_api.requests, "post", boom)
    assert rms_api._get_secret("RMS_LICENSE_KEY") == "OLDKEY0000"
    src = rms_api.key_source()
    assert src["source"] == "secrets" and "ConnectionError" in src["detail"]


def test_wrong_decrypt_key_falls_back(monkeypatch):
    configure(monkeypatch)
    other = Fernet.generate_key()
    bad = [{"ciphertext": Fernet(other).encrypt(b"NEWKEY6NUX").decode(), "last4": "6NUX"}]
    monkeypatch.setattr(rms_api.requests, "post", lambda *a, **k: Resp(bad))
    assert rms_api._get_secret("RMS_LICENSE_KEY") == "OLDKEY0000"


def test_not_configured_uses_secrets_without_network(monkeypatch):
    monkeypatch.setenv("RMS_LICENSE_KEY", "OLDKEY0000")
    monkeypatch.setattr(rms_api.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("通信した")))
    assert rms_api._get_secret("RMS_LICENSE_KEY") == "OLDKEY0000"
    assert "未設定" in rms_api.key_source()["detail"]


def test_other_secrets_never_touch_the_store(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("RMS_SERVICE_SECRET", "SVC")
    monkeypatch.setattr(rms_api.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("通信した")))
    assert rms_api._get_secret("RMS_SERVICE_SECRET") == "SVC"


def test_public_logs_never_contain_the_key(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(rms_api.requests, "post", lambda *a, **k: Resp(row("NEWKEY6NUX")))
    rms_api._get_secret("RMS_LICENSE_KEY")
    out = capsys.readouterr().out
    assert "共通の置き場" in out and "NEWKEY" not in out and "6NUX" not in out
