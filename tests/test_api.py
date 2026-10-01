import json
import time

import pytest
from fastapi.testclient import TestClient

from signaldesk.api import create_app
from signaldesk.config import Config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    monkeypatch.setenv("SIGNALDESK_SCHEDULER", "0")
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


# --- WF-5: outcomes dashboard + paper execution log --------------------------

def _seed_signal(tmp_path, **over):
    from signaldesk import ledger as ledger_mod

    base = dict(signal_id="run1:BTCUSD", created_at="2026-09-02T00:00:00+00:00",
                run_id="run1", market="crypto", symbol="BTCUSD", score=80.0,
                entry=100.0, stop=95.0, tp1=110.0, tp2=115.0,
                risk=5.0, risk_pct=5.0, cost_pct=0.25, cost_in_r=0.05,
                bars_last_date="2026-06-01")
    base.update(over)
    rec = ledger_mod.LedgerRecord(**base)
    ledger_mod.append_records(ledger_mod.ledger_path(tmp_path), [rec])
    return rec


def test_outcomes_endpoint_empty(client):
    data = client.get("/api/outcomes").json()
    assert data["metrics"]["n_rows"] == 0
    assert data["signals"] == [] and data["resolutions"] == []
    assert data["stale"] is False          # nothing to resolve: no pending state
    assert data["scheduler"] == {}


def test_outcomes_resolve_and_cached_view(client):
    _seed_signal(client.app.state.config.data_dir)
    data = client.post("/api/outcomes/resolve", json={"demo": True}).json()
    assert data["metrics"]["n_rows"] == 1
    assert data["resolutions"][0]["status"] in ("tp1", "tp2", "sl", "time")
    assert data["stale"] is False

    cached = client.get("/api/outcomes").json()
    assert cached["metrics"]["n_rows"] == 1
    assert cached["signals"][0]["symbol"] == "BTCUSD"
    assert cached["signals"][0]["status"] == data["resolutions"][0]["status"]


def test_paper_flow_via_api(client):
    data_dir = client.app.state.config.data_dir
    rec = _seed_signal(data_dir)

    unknown = client.post("/api/paper/nope:BTCUSD/fill", json={"price": 101}).json()
    assert "error" in unknown

    fill = client.post(f"/api/paper/{rec.signal_id}/fill", json={"price": 101}).json()
    assert fill["event"] == "fill" and fill["price"] == 101

    data = client.get("/api/outcomes").json()
    assert data["paper"]["fills"] == 1
    assert data["paper"]["mean_entry_gap_r"] == pytest.approx(-0.2)  # (100-101)/5
    assert data["signals"][0]["paper"]["event"] == "fill"


def test_paper_miss_and_fill_rate(client):
    data_dir = client.app.state.config.data_dir
    rec = _seed_signal(data_dir)
    other = _seed_signal(data_dir, signal_id="run1:ETHUSD", symbol="ETHUSD")
    client.post(f"/api/paper/{rec.signal_id}/fill", json={"price": 100})
    client.post(f"/api/paper/{other.signal_id}/miss", json={})
    data = client.get("/api/outcomes").json()
    assert data["paper"]["fill_rate"] == 0.5
    assert data["paper"]["misses"] == 1


def test_scheduler_runs_daily_pass_on_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_SCHEDULER", "1")
    with TestClient(create_app(Config(data_dir=tmp_path))) as c:
        for _ in range(50):                    # background thread: poll briefly
            data = c.get("/api/outcomes").json()
            if data["scheduler"].get("last_status"):
                assert data["scheduler"]["last_status"] == "ok"
                assert data["scheduler"]["last_resolution_date"] is not None
                return
            time.sleep(0.1)
    pytest.fail("scheduler never ran its startup pass")


def test_chart_served_while_run_is_running(client):
    """Live-trace chart images: the store row only learns the run dir at
    finish_run, so the chart endpoint must fall back to the in-memory hub
    dir — otherwise every P4 chart 404s until the run completes."""
    from signaldesk.api import create_app  # noqa: F401 (app already built)

    _PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082"
    )
    store = client.app.state.store
    hub = client.app.state.runs
    row = store.create_run(session_id="", prompt_id="", workflow="market_scan",
                           market="crypto", universe=["BTCUSD"])
    run_dir = client.app.state.config.run_dir("chart-live-test")
    charts = run_dir / "sandbox" / "output" / "charts"
    charts.mkdir(parents=True)
    (charts / "BTCUSD-1d.png").write_bytes(_PNG)
    hub.set_run_dir(row.id, str(run_dir))

    resp = client.get(f"/api/runs/{row.id}/charts/BTCUSD-1d.png")
    assert resp.status_code == 200, resp.json()
    assert resp.headers["content-type"] == "image/png"

    # without a hub dir and without a persisted run_dir there is no chart
    row2 = store.create_run(session_id="", prompt_id="", workflow="market_scan",
                            market="crypto", universe=["BTCUSD"])
    missing = client.get(f"/api/runs/{row2.id}/charts/BTCUSD-1d.png")
    assert missing.status_code == 200 and missing.json() == {"error": "run not found"}
