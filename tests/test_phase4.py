"""Phase 4: update-check logic + onboarding endpoints."""
import json

import pytest

from signaldesk.api import create_app
from signaldesk.config import Config
from signaldesk.updates import _parse_version, check_for_update


def test_parse_version():
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("1.2.0b1") == (1, 2, 0)
    assert (1, 0, 1) > (1, 0, 0)


def test_update_check_newer_version(monkeypatch):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"version": "99.0.0", "url": "https://x", "notes": "big release"}

    monkeypatch.setattr("signaldesk.updates.httpx.get", lambda *a, **k: FakeResp())
    info = check_for_update(manifest_url="https://example.test/manifest.json")
    assert info.update_available is True
    assert info.latest == "99.0.0"
    assert info.url == "https://x"


def test_update_check_same_version_no_update(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            import signaldesk
            return {"version": signaldesk.__version__, "url": "https://x"}

    monkeypatch.setattr("signaldesk.updates.httpx.get", lambda *a, **k: FakeResp())
    info = check_for_update(manifest_url="https://example.test/manifest.json")
    assert info.update_available is False


def test_update_check_offline_degrades(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("offline")

    monkeypatch.setattr("signaldesk.updates.httpx.get", boom)
    info = check_for_update(manifest_url="https://unreachable.test/manifest.json")
    assert info.update_available is False
    assert info.latest == info.current


def test_onboarding_endpoints(client_settings):
    got = client_settings.get("/api/onboarding").json()
    assert got == {"done": False, "consent_prompts": False}
    client_settings.post("/api/onboarding/finish")
    assert client_settings.get("/api/onboarding").json()["done"] is True


def test_update_check_endpoint(client_settings):
    got = client_settings.get("/api/update-check").json()
    assert "version" not in got or got  # shape check
    assert {"current", "latest", "update_available", "url", "notes"} <= set(got)


@pytest.fixture()
def client_settings(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    cfg = Config(data_dir=tmp_path)
    with TestClient(create_app(cfg)) as c:
        yield c
