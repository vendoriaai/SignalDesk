import json
import time

import pytest
from fastapi.testclient import TestClient

from signaldesk.api import create_app
from signaldesk.config import Config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    cfg = Config(data_dir=tmp_path)
    with TestClient(create_app(cfg)) as c:
        yield c


def _wait_done(client, run_id, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["status"] in ("done", "failed"):
            return detail
        time.sleep(0.2)
    raise TimeoutError("run never finished")


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_chat_demo_scan_end_to_end(client):
    resp = client.post("/api/chat", json={"prompt": "scan crypto", "demo": True}).json()
    assert "run_id" in resp, resp
    assert resp["plan"]["workflow"] == "market_scan"
    detail = _wait_done(client, resp["run_id"])
    assert detail["status"] == "done", detail.get("error")
    assert "Ranked signals" in detail["report_md"]
    assert detail["report_json"]["citation_coverage"] == 1.0
    assert len(detail["report_json"]["citations"]) >= 20
    assert detail["events"], "expected streamed events buffered"

    # history records it
    runs = client.get("/api/runs").json()
    assert any(r["id"] == resp["run_id"] for r in runs)
    hist = client.get("/api/history").json()
    assert hist["signals"], "signals must be persisted"


def test_chat_deepdive_demo(client):
    resp = client.post("/api/chat", json={"prompt": "deep dive AAPL", "demo": True}).json()
    assert resp["plan"]["symbol"] == "AAPL"
    detail = _wait_done(client, resp["run_id"])
    assert detail["status"] == "done"
    assert "Deep Dive: AAPL" in detail["report_md"]


def test_scan_endpoint_demo(client):
    resp = client.post("/api/scan", json={"market": "forex", "demo": True, "universe_size": 6}).json()
    detail = _wait_done(client, resp["run_id"])
    assert detail["status"] == "done", detail.get("error")
    assert detail["report_json"]["scoring_preset"] == "fx-momentum-v1"


def test_websocket_replays_buffered_events(client):
    resp = client.post("/api/scan", json={"market": "crypto", "demo": True, "universe_size": 4}).json()
    detail = _wait_done(client, resp["run_id"])
    assert detail["status"] == "done"
    with client.websocket_connect(f"/ws/runs/{resp['run_id']}") as ws:
        received = []
        while True:
            msg = json.loads(ws.receive_text())
            received.append(msg)
            if msg.get("kind") == "done":
                break
        assert any(m.get("kind") == "sandbox" for m in received)
        assert received[-1]["status"] == "done"


def test_watchlist_api_flow(client):
    client.post("/api/watchlists", json={"name": "wl1", "symbols": ["BTCUSD", "ETHUSD"], "market": "crypto"})
    lists = client.get("/api/watchlists").json()
    assert lists and lists[0]["name"] == "wl1"
    client.post("/api/watchlists/wl1/symbols", json={"add": ["SOLUSD"], "remove": ["BTCUSD"]})
    lists = client.get("/api/watchlists").json()
    assert lists[0]["symbols"] == ["ETHUSD", "SOLUSD"]
    resp = client.post("/api/watchlists/wl1/scan", params={"demo": True}).json()
    detail = _wait_done(client, resp["run_id"])
    assert detail["status"] == "done"
    assert set(detail["report_json"]["universe"]) == {"ETHUSD", "SOLUSD"}


def test_run_detail_replays_trace_after_restart(client, tmp_path):
    """History after a server restart: events come back from the persisted
    trace.jsonl (not the in-memory RunManager), and charts still resolve."""
    resp = client.post("/api/scan", json={"market": "crypto", "demo": True, "universe_size": 2}).json()
    detail = _wait_done(client, resp["run_id"])
    assert detail["status"] == "done", detail.get("error")

    # fresh process state over the same data dir: RunManager is empty
    cfg2 = Config(data_dir=tmp_path)
    with TestClient(create_app(cfg2)) as c2:
        again = c2.get(f"/api/runs/{resp['run_id']}").json()
    assert again["status"] == "done"
    assert again["live_status"] == "done"
    assert again["events"], "persisted trace must replay for history"
    assert any(e["kind"] == "sandbox" for e in again["events"])
    assert again["report_md"]
    from pathlib import Path

    for sym, tfs in (again["report_json"].get("charts") or {}).items():
        for rel in tfs.values():
            assert (Path(again["run_dir"]) / rel).is_file(), f"missing chart {rel}"


def test_settings_roundtrip_and_masking(client, monkeypatch):
    import keyring
    from keyring.backend import KeyringBackend

    monkeypatch.setattr("signaldesk.userconfig.get_secret", lambda k: "sk-test-123456" if k == "OPENAI_API_KEY" else None, raising=False)
    # simplest: monkeypatch module functions to an in-memory dict
    vault = {}
    monkeypatch.setattr("signaldesk.userconfig.set_secret", lambda k, v: vault.__setitem__(k, v))
    monkeypatch.setattr("signaldesk.userconfig.delete_secret", lambda k: vault.pop(k, None))
    monkeypatch.setattr(
        "signaldesk.userconfig.masked_secret",
        lambda k: (vault[k][:3] + "…" + vault[k][-2:]) if vault.get(k) else None,
    )
    body = {"consent_prompts": True, "llm_provider": "anthropic", "secrets": {"OPENAI_API_KEY": "sk-test-123456"}}
    got = client.post("/api/settings", json=body).json()
    assert got["consent_prompts"] is True
    assert got["llm_provider"] == "anthropic"
    assert got["secrets"]["OPENAI_API_KEY"].startswith("sk-")
    # persisted?
    got2 = client.get("/api/settings").json()
    assert got2["consent_prompts"] is True


def test_sync_refused_without_consent(client):
    resp = client.post("/api/sync")
    assert "consent off" in resp.json()["error"]
