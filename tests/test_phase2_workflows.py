"""End-to-end: WF-3 forex/metals demo scans, WF-2 demo deep dive, WF-4
watchlist scan with diff, and the 30-case eval harness."""
from signaldesk.agent.events import EventBus
from signaldesk.tools import demo, demo_equities
from signaldesk.workflows.deep_dive import DeepDiveRequest, DeepDiveToolSet, run_deep_dive
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan


def _fx_tools(d, market):
    return ToolSet(
        movers=demo.DemoMoversTool(d, market),
        quotes=demo.DemoQuotesTool(d, market),
        ohlcv=demo.DemoOHLCVTool(d, market),
        search=demo.DemoSearchTool(),
        macro=demo.DemoMacroTool(d, market),
    )


def test_forex_scan_demo_end_to_end(tmp_path):
    run = run_market_scan(
        MarketScanRequest(market="forex", universe_size=10),
        _fx_tools(tmp_path / "artifacts", "forex"),
        EventBus(), tmp_path,
    )
    r = run.report
    assert r.market == "forex"
    assert r.scoring_preset == "fx-momentum-v1"
    # macro indices must be reflected in sentiment section (WF-3)
    indices = {s.index for s in r.sentiment}
    assert {"dxy_30d", "us10y_30d"} <= indices
    assert r.citation_coverage == 1.0
    # FX signals carry a session note with pips (WF-3)
    for s in r.signals:
        assert any("pip" in c.lower() or "session" in c.lower() for c in s.confluence)


def test_metals_scan_demo(tmp_path):
    run = run_market_scan(
        MarketScanRequest(market="metals", universe_size=2),
        _fx_tools(tmp_path / "artifacts", "metals"),
        EventBus(), tmp_path,
    )
    assert set(run.report.universe) == {"XAUUSD", "XAGUSD"}
    assert run.report.scoring_preset == "fx-momentum-v1"


def test_deepdive_demo_equity(tmp_path):
    tools = DeepDiveToolSet(
        quotes=demo.DemoQuotesTool(tmp_path / "artifacts", "equities"),
        ohlcv=demo.DemoOHLCVTool(tmp_path / "artifacts", "equities"),
        search=demo.DemoSearchTool(),
        fundamentals=demo_equities.DemoFundamentalsTool(tmp_path / "artifacts"),
        edgar=demo_equities.DemoEdgarTool(tmp_path / "artifacts"),
        earnings=demo_equities.DemoEarningsTool(tmp_path / "artifacts"),
    )
    run = run_deep_dive(DeepDiveRequest(symbol="AAPL"), tools, EventBus(), tmp_path)
    r = run.report
    assert r.symbol == "AAPL"
    assert r.technicals and "rsi14" in r.technicals
    assert r.fundamentals.name and r.fundamentals.market_cap
    assert r.annuals, "expected EDGAR annuals in demo"
    assert r.earnings_next, "expected next earnings date"
    assert r.citations, "deep dive must cite"
    assert "Deep Dive: AAPL" in run.report_md
    assert "Not financial advice" in run.report_md


def test_watchlist_scan_and_diff(tmp_path, monkeypatch):
    from signaldesk import watchlists

    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    wl = watchlists.add_symbols(tmp_path, "growth", ["BTCUSD", "ETHUSD", "SOLUSD"], market="crypto")
    assert wl.symbols

    tools = ToolSet(
        movers=demo.DemoMoversTool(tmp_path / "artifacts", "crypto"),
        quotes=demo.DemoQuotesTool(tmp_path / "artifacts", "crypto"),
        ohlcv=demo.DemoOHLCVTool(tmp_path / "artifacts", "crypto"),
        search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(tmp_path / "artifacts"),
        altseason=demo.DemoAltSeasonTool(tmp_path / "artifacts"),
    )
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_override=wl.symbols, watchlist_name="growth"),
        tools, EventBus(), tmp_path / "run1",
    )
    assert set(run.report.universe) == {"BTCUSD", "ETHUSD", "SOLUSD"}
    assert all(s.symbol in wl.symbols for s in run.report.signals)


def test_eval_harness_all_pass():
    from signaldesk.evals.runner import evaluate

    results, score = evaluate()
    fails = [r for r in results if not r.ok]
    for r in fails:
        print(f"FAIL case {r.case_id}: {r.prompt}  {r.detail}  {r.checks}")
    assert score == 1.0
    assert len(results) == 30


def test_multi_timeframe_charts(tmp_path):
    """Every symbol gets a chart per timeframe; confluence cites LTF alignment."""
    import pandas as pd  # noqa: F401
    from signaldesk.agent.events import EventBus
    from signaldesk.tools import demo
    from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

    d = tmp_path / "artifacts"
    tools = ToolSet(
        movers=demo.DemoMoversTool(d, "crypto"),
        quotes=demo.DemoQuotesTool(d, "crypto"),
        ohlcv=demo.DemoOHLCVTool(d, "crypto"),
        search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(d),
        altseason=demo.DemoAltSeasonTool(d),
    )
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4), tools,
                          EventBus(), tmp_path)
    charts = run.report.charts
    assert charts, "no charts rendered"
    for sym, tfs in charts.items():
        assert "1d" in tfs, f"{sym} missing daily chart"
        assert "1h" in tfs, f"{sym} missing 1h chart"
        for rel in tfs.values():
            assert (tmp_path / rel).exists(), f"missing file {rel}"
    # LTF confluence shows up in the signals when the 1h trend agrees
    signals_txt = " ".join(r for s in run.report.signals for r in s.confluence)
    assert ("1h trend aligned" in signals_txt) or ("1h trend divergent" in signals_txt)
