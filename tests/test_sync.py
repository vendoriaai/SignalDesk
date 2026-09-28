"""Sync engine tests — all HTTP monkeypatched; no network."""
import json
from datetime import UTC, datetime

from signaldesk.store import LocalStore
from signaldesk.sync import SyncEngine, SupabaseClient, SupabaseConfig


class FakeHTTP:
    """Stand-in for httpx.post/get used by SupabaseClient."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.watchlists_rows: list[dict] = []
        self.items_rows: list[dict] = []

    class _Resp:
        def __init__(self, payload, status=200, url=""):
            self._payload = payload
            self.status_code = status
            self.text = json.dumps(payload) if payload is not None else ""

        def json(self):
            return self._payload

    def post(self, url, json=None, headers=None, params=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/token?grant_type=password") or url.endswith("/signup"):
            return self._Resp({"access_token": "at", "refresh_token": "rt",
                               "user": {"id": "u1", "email": "a@b.c"}})
        if "/rpc/delete_my_data" in url:
            return self._Resp({})
        return self._Resp([])  # upserts

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        if "/watchlist_items" in url:
            return self._Resp(self.items_rows)
        if "/watchlists" in url:
            return self._Resp(self.watchlists_rows)
        return self._Resp([])


def _engine(tmp_path, monkeypatch):
    fake = FakeHTTP()
    import signaldesk.sync as sync_mod

    monkeypatch.setattr(sync_mod.httpx, "post", fake.post)
    monkeypatch.setattr(sync_mod.httpx, "get", fake.get)
    client = SupabaseClient(SupabaseConfig(url="https://sb.test", anon_key="k"))
    store = LocalStore(tmp_path)
    return fake, client, store, SyncEngine(client, store)


def test_login_captures_tokens(tmp_path, monkeypatch):
    fake, client, *_ = _engine(tmp_path, monkeypatch)
    auth = client.sign_in("a@b.c", "pw")
    assert auth.logged_in and auth.user_id == "u1"
    assert any("/token?grant_type=password" in u for _, u, _ in fake.calls)


def test_consent_off_pushes_nothing(tmp_path, monkeypatch):
    fake, client, store, engine = _engine(tmp_path, monkeypatch)
    client.sign_in("a@b.c", "pw")
    store.add_prompt("sess", "scan crypto")
    out = engine.push_all(consent=False)
    assert out["uploaded"] == 0
    assert not any("/runs" in u or "/prompts" in u for _, u, _ in fake.calls)


def test_push_all_with_consent(tmp_path, monkeypatch):
    fake, client, store, engine = _engine(tmp_path, monkeypatch)
    client.sign_in("a@b.c", "pw")
    p = store.add_prompt("s", "scan crypto")
    r = store.create_run(session_id="s", prompt_id=p.id, workflow="market_scan",
                         market="crypto", universe=["BTCUSD"])
    store.finish_run(r.id, report_md="# rep", report_json="{}", trace_jsonl="", scoring_preset="trend-momentum-v1")
    store.add_signals(r.id, "crypto", [dict(symbol="BTCUSD", direction="LONG", score=90.0,
                                           entry=100.0, stop=90.0, tp1=120.0, tp2=130.0)])
    out = engine.push_all(consent=True)
    tables = {u.split("/rest/v1/")[-1] for _, u, _ in fake.calls if "/rest/v1/" in u}
    assert tables >= {"runs", "prompts", "reports", "signals"}
    assert out["uploaded"] >= 1


def test_pull_watchlists_merges(tmp_path, monkeypatch):
    fake, client, store, engine = _engine(tmp_path, monkeypatch)
    client.sign_in("a@b.c", "pw")
    fake.watchlists_rows = [{"id": "w1", "name": "growth", "updated_at": datetime.now(UTC).isoformat()}]
    fake.items_rows = [{"watchlist_id": "w1", "symbol": "BTCUSD"}, {"watchlist_id": "w1", "symbol": "SOLUSD"}]
    from signaldesk import watchlists as wl_mod

    wl_mod.add_symbols(tmp_path, "growth", ["ETHUSD"])
    out = engine.pull_watchlists(tmp_path)
    assert out["pulled"] == 1
    merged = wl_mod.load(tmp_path, "growth")
    assert set(merged.symbols) == {"BTCUSD", "ETHUSD", "SOLUSD"}


def test_delete_my_data_calls_rpc(tmp_path, monkeypatch):
    fake, client, *_ = _engine(tmp_path, monkeypatch)
    client.sign_in("a@b.c", "pw")
    client.delete_my_data()
    assert any("/rpc/delete_my_data" in u for _, u, _ in fake.calls)
