"""End-to-end WF-1 test on the offline demo market (no network)."""
import pandas as pd

from signaldesk.agent.events import EventBus
from signaldesk.tools import demo
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan


def _demo_tools(artifacts_dir):
    return ToolSet(
        movers=demo.DemoMoversTool(artifacts_dir),
        quotes=demo.DemoQuotesTool(artifacts_dir),
        ohlcv=demo.DemoOHLCVTool(artifacts_dir),
        fear_greed=demo.DemoFearGreedTool(artifacts_dir),
        altseason=demo.DemoAltSeasonTool(artifacts_dir),
        search=demo.DemoSearchTool(),
    )


def test_market_scan_end_to_end(tmp_path):
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=12),
        _demo_tools(tmp_path / "artifacts"),
        bus,
        tmp_path,
    )
    report = run.report

    # golden assertions from roadmap Phase 1 exit test
    assert report.market == "crypto"
    assert len(report.citations) >= 20
    assert report.citation_coverage == 1.0
    assert report.signals, "expected at least one signal in demo market"
    assert len(report.signals) <= 5

    # R3/R5: no signal may be RSI-overextended, and every signal is cited
    for signal in report.signals:
        assert signal.score >= 60
        assert signal.citations
        assert signal.stop < signal.entry < signal.tp1 < signal.tp2
        rsi_cite = next(c for c in signal.citations if "RSI" in report.citations[c].label)
        assert report.citations[rsi_cite].value < 75

    # persisted artifacts
    assert (run.run_dir / "report.md").exists()
    assert (run.run_dir / "report.json").exists()
    assert (run.run_dir / "trace.jsonl").exists()
    assert "Not financial advice" in run.report_md
    assert bus.events, "trace must be non-empty"


def test_overextended_symbol_goes_to_avoid(tmp_path):
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=12),
        _demo_tools(tmp_path / "artifacts"),
        bus,
        tmp_path,
    )
    # the demo market's parabolic runner must never show up as a signal
    signal_symbols = {s.symbol for s in run.report.signals}
    features = pd.read_csv(tmp_path / "sandbox" / "output" / "features.csv", index_col=0)
    for sym, row in features.iterrows():
        if row["rsi14"] >= 75:
            assert sym not in signal_symbols


def test_unknown_market_rejected(tmp_path):
    import pytest

    bus = EventBus()
    with pytest.raises(ValueError, match="unknown market"):
        run_market_scan(
            MarketScanRequest(market="commodities"),
            _demo_tools(tmp_path / "artifacts"),
            bus,
            tmp_path,
        )