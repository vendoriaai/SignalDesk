"""Universe integrity (roadmap item 29): liquidity screen + point-in-time
survivorship audit."""
import json
from datetime import UTC, datetime, timedelta

import pandas as pd
from typer.testing import CliRunner

from signaldesk import ledger, universe

runner = CliRunner()


def _rec(signal_id: str, symbol: str, *, demo: bool = False) -> ledger.LedgerRecord:
    return ledger.LedgerRecord(
        signal_id=signal_id, created_at="2026-08-01T00:00:00+00:00",
        run_id=signal_id.split(":")[0], market="crypto", symbol=symbol,
        score=80.0, entry=100.0, stop=95.0, tp1=110.0, tp2=115.0,
        risk=5.0, risk_pct=5.0, cost_pct=0.25, cost_in_r=0.05,
        bars_last_date="2026-07-31", demo=demo,
    )


def _movers_rows() -> list[dict]:
    return [
        {"symbol": "BTCUSD", "price": 112000.0, "change_24h_pct": 2.0,
         "volume": 2.0e9, "market_cap": 5.0e11, "side": "gainer"},
        {"symbol": "SUIUSD", "price": 3.6, "change_24h_pct": 9.0,
         "volume": 1.9e8, "market_cap": 1.0e10, "side": "gainer"},
        {"symbol": "SCAMUSD", "price": 0.03, "change_24h_pct": 40.0,
         "volume": 8.0e5, "market_cap": 9.0e6, "side": "gainer"},
        {"symbol": "LINKUSD", "price": 22.0, "change_24h_pct": 4.0,
         "volume": None, "market_cap": None, "side": "gainer"},
    ]


# ---- liquidity screen -------------------------------------------------------

def test_screen_drops_illiquid_crypto_rows_and_keeps_missing_data():
    df = pd.DataFrame(_movers_rows())
    kept, dropped = universe.liquidity_screen(df, "crypto")
    assert kept["symbol"].tolist() == ["BTCUSD", "SUIUSD", "LINKUSD"]
    assert [d["symbol"] for d in dropped] == ["SCAMUSD"]
    assert "volume" in dropped[0]["reason"]   # both floors fire; volume named first


def test_screen_flags_either_floor():
    df = pd.DataFrame([
        {"symbol": "THINUSD", "volume": 9.0e9, "market_cap": 1.0e6, "side": "gainer"},
        {"symbol": "QUIETUSD", "volume": 10.0, "market_cap": 9.0e10, "side": "gainer"},
    ])
    _, dropped = universe.liquidity_screen(df, "crypto")
    by_sym = {d["symbol"]: d["reason"] for d in dropped}
    assert "market cap" in by_sym["THINUSD"]
    assert "volume" in by_sym["QUIETUSD"]


def test_screen_leaves_tapeless_markets_untouched():
    df = pd.DataFrame([{"symbol": "EURUSD", "volume": 0.0, "market_cap": None,
                        "side": "gainer"}])
    kept, dropped = universe.liquidity_screen(df, "forex")
    assert dropped == []
    assert kept["symbol"].tolist() == ["EURUSD"]


# ---- screen wired into the scan (live movers path, demo data offline) -------

def test_scan_applies_liquidity_screen_to_movers(tmp_path):
    from signaldesk.agent.events import EventBus
    from signaldesk.tools import demo
    from signaldesk.tools.base import Source, Tool, ToolResult
    from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

    class StubMovers(Tool):
        name = "market_movers"

        def __init__(self, d):
            self.d = d
            d.mkdir(parents=True, exist_ok=True)

        def run(self, per_side=10):
            path = self.d / "movers.csv"
            pd.DataFrame(_movers_rows()).to_csv(path, index=False)
            return ToolResult(csv_files=[path], summary="stub movers",
                              sources=[Source(name="stub")])

    class StubOHLCV(demo.DemoOHLCVTool):
        pass  # demo data under a non-Demo class name, so the screen applies

    tools = ToolSet(
        movers=StubMovers(tmp_path / "artifacts"),
        quotes=demo.DemoQuotesTool(tmp_path / "artifacts", "crypto"),
        ohlcv=StubOHLCV(tmp_path / "artifacts", "crypto"),
        search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(tmp_path / "artifacts"),
        altseason=demo.DemoAltSeasonTool(tmp_path / "artifacts"),
    )
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=12),
                          tools, EventBus(), tmp_path)
    r = run.report
    assert "SCAMUSD" not in r.universe
    assert "SUIUSD" in r.universe          # liquid gainer survived the screen
    assert "LINKUSD" in r.universe         # missing liquidity data is kept (R4)
    assert any("Liquidity screen" in d and "SCAMUSD" in d for d in r.disclosures)
    screen_cites = [c for c in r.citations.values()
                    if c.source_tool == "liquidity_screen"]
    assert len(screen_cites) == 2          # volume + market-cap floors, cited (R1)


# ---- point-in-time universe audit -------------------------------------------

def _write_run(data_dir, run_id, market, symbols, as_of):
    d = data_dir / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(
        json.dumps({"market": market, "as_of": as_of, "universe": symbols}),
        encoding="utf-8")


def _stub_providers(today):
    def bars(days_ago):
        d = today - timedelta(days=days_ago)
        return pd.DataFrame({"date": [str(d - timedelta(days=k)) for k in range(3)],
                             "open": [1.0] * 3, "high": [1.0] * 3,
                             "low": [1.0] * 3, "close": [1.0] * 3})

    table = {"ACTUSD": bars(1), "OLDUSD": bars(40), "DEADUSD": pd.DataFrame()}
    return {"crypto": lambda sym: table[sym]}


def test_audit_classifies_active_dormant_and_gone(tmp_path):
    today = datetime.now(UTC).date()
    _write_run(tmp_path, "20260801T1200", "crypto",
               ["ACTUSD", "OLDUSD", "DEADUSD"], "2026-08-01T12:00:00+00:00")
    _write_run(tmp_path, "20260901T1200", "crypto", ["ACTUSD"],
               "2026-09-01T12:00:00+00:00")
    ledger.append_records(ledger.ledger_path(tmp_path),
                          [_rec("r1:ACTUSD", "ACTUSD"), _rec("r2:DEADUSD", "DEADUSD")])

    audit = universe.run_audit(tmp_path, providers=_stub_providers(today), today=today)

    assert audit.runs == 2
    assert audit.first_run == "2026-08-01" and audit.last_run == "2026-09-01"
    assert audit.scanned_symbols == 3
    assert (audit.active, audit.dormant, audit.no_data) == (1, 1, 1)
    assert audit.signal_symbols == 2 and audit.signal_symbols_gone == 1
    assert audit.survivorship_exposure == 0.5
    rows = {r["symbol"]: r for r in audit.rows}
    assert rows["ACTUSD"]["runs"] == 2
    assert rows["DEADUSD"]["status"] == "no_data" and rows["DEADUSD"]["signals"] == 1
    assert rows["OLDUSD"]["status"] == "dormant"
    assert any("survivor" in n for n in audit.notes)


def test_audit_market_filter(tmp_path):
    today = datetime.now(UTC).date()
    _write_run(tmp_path, "20260801T1200", "forex", ["EURUSD"], "2026-08-01T12:00:00+00:00")
    _write_run(tmp_path, "20260802T1200", "crypto", ["ACTUSD"], "2026-08-02T12:00:00+00:00")
    audit = universe.run_audit(tmp_path, market="forex",
                               providers=_stub_providers(today), today=today)
    assert audit.scanned_symbols == 1
    assert audit.rows[0]["symbol"] == "EURUSD"


def test_demo_runs_and_signals_excluded_unless_asked(tmp_path):
    today = datetime.now(UTC).date()
    _write_run(tmp_path, "20260801T1200-demo", "crypto", ["BTCUSD"],
               "2026-08-01T12:00:00+00:00")
    ledger.append_records(ledger.ledger_path(tmp_path),
                          [_rec("r:BTCUSD", "BTCUSD", demo=True)])
    audit = universe.run_audit(tmp_path, providers=_stub_providers(today), today=today)
    assert audit.scanned_symbols == 0 and audit.signal_symbols == 0

    audit_demo = universe.run_audit(tmp_path, demo=True,
                                    providers=_stub_providers(today), today=today)
    assert audit_demo.scanned_symbols == 1


def test_audit_persists_and_bias_note(tmp_path):
    assert universe.bias_note(tmp_path) is None          # no audit yet
    today = datetime.now(UTC).date()
    _write_run(tmp_path, "20260801T1200", "crypto",
               ["ACTUSD", "DEADUSD"], "2026-08-01T12:00:00+00:00")
    ledger.append_records(ledger.ledger_path(tmp_path),
                          [_rec("r1:ACTUSD", "ACTUSD"), _rec("r2:DEADUSD", "DEADUSD")])
    audit = universe.run_audit(tmp_path, providers=_stub_providers(today), today=today)
    universe.save_audit(tmp_path, audit)

    note = universe.bias_note(tmp_path)
    assert note and "1/2" in note and "survivor-biased" in note

    loaded = universe.load_audit(tmp_path)
    assert loaded["scanned_symbols"] == 2


def test_render_audit_text_mentions_gone_names(tmp_path):
    today = datetime.now(UTC).date()
    _write_run(tmp_path, "20260801T1200", "crypto", ["DEADUSD"],
               "2026-08-01T12:00:00+00:00")
    audit = universe.run_audit(tmp_path, providers=_stub_providers(today), today=today)
    text = universe.render_audit_text(audit)
    assert "DEADUSD" in text and "no_data" in text


# ---- CLI ---------------------------------------------------------------------

def test_universe_command_json(tmp_path, monkeypatch):
    from signaldesk.cli import app

    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    _write_run(tmp_path, "20260801T1200", "crypto", ["ACTUSD", "DEADUSD"],
               "2026-08-01T12:00:00+00:00")
    ledger.append_records(ledger.ledger_path(tmp_path), [_rec("r1:ACTUSD", "ACTUSD")])

    result = runner.invoke(app, ["universe", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_symbols"] == 2
    assert payload["signal_symbols"] == 1 and payload["signal_symbols_gone"] == 0
    assert (tmp_path / "universe_audit.json").is_file()
