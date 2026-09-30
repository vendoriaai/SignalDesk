"""Phase 7.5 intraday entry refinement: demo E2E, off-switch, degradation."""
from signaldesk.agent.events import EventBus
from signaldesk.tools import demo
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan


def _tools(d, market="crypto"):
    return ToolSet(
        movers=demo.DemoMoversTool(d, market),
        quotes=demo.DemoQuotesTool(d, market),
        ohlcv=demo.DemoOHLCVTool(d, market),
        search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(d),
        altseason=demo.DemoAltSeasonTool(d),
    )


def _scan(tmp_path, **kwargs):
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=4, **kwargs),
        _tools(tmp_path / "artifacts"), EventBus(), tmp_path,
    )
    return run.report


def test_entry_plans_attached_and_cited(tmp_path):
    """LONG demo signals get a deterministic entry plan; SHORT signals keep
    their daily plan in demo (7.5 is long-only, the 7.6 AI read is offline)."""
    report = _scan(tmp_path)
    assert report.signals, "demo scan must produce signals"
    for s in report.signals:
        p = s.entry_plan
        if s.direction == "SHORT":
            assert p is None, f"{s.symbol} SHORT must keep its daily plan in demo"
            assert s.tp2 < s.tp1 < s.entry < s.stop, f"{s.symbol} short geometry broken"
            continue
        assert p is not None, f"{s.symbol} missing entry plan"
        assert p.mode in ("market", "pullback", "wait")
        assert p.stop < p.entry < p.tp1 < p.tp2, f"{s.symbol} plan geometry broken"
        assert p.citations or not p.timeframes, "plan numbers/snapshot must be cited"
        for cid in p.citations:
            assert cid in report.citations, f"{s.symbol} plan cite {cid} unregistered"
    assert report.citation_coverage == 1.0


def test_refined_risk_is_bounded(tmp_path):
    """Refinement never zeroes out or wildly inflates the risk per share."""
    report = _scan(tmp_path)
    for s in report.signals:
        p = s.entry_plan
        if p is not None and p.mode in ("market", "pullback"):
            assert 0 < p.risk_refined < p.risk_daily * 3


def test_entry_chart_rendered_and_registered(tmp_path):
    report = _scan(tmp_path)
    entry_charts = [(sym, tf, rel) for sym, tfs in report.charts.items()
                    for tf, rel in tfs.items() if tf.endswith("-entry")]
    assert entry_charts, "expected at least one intraday entry chart"
    for sym, tf, rel in entry_charts:
        assert (tmp_path / rel).is_file(), f"missing chart file for {sym}-{tf}"


def test_entry_plan_markdown_section(tmp_path):
    report = _scan(tmp_path)
    from signaldesk.report.render import to_markdown

    md = to_markdown(report)
    if any(s.entry_plan is not None for s in report.signals):
        assert "## Entry plans (intraday refinement)" in md
    for s in report.signals:
        assert s.symbol in md


def test_entry_refinement_disabled(tmp_path):
    report = _scan(tmp_path, entry_timeframes=[])
    assert report.signals
    assert all(s.entry_plan is None for s in report.signals)
    assert not any(tf.endswith("-entry") for tfs in report.charts.values() for tf in tfs)


def test_custom_entry_timeframes_subset(tmp_path):
    report = _scan(tmp_path, entry_timeframes=["15m"])
    for s in report.signals:
        if s.direction == "SHORT":
            assert s.entry_plan is None   # 7.5 is long-only; demo has no AI read
            continue
        assert s.entry_plan is not None
        assert set(s.entry_plan.timeframes) <= {"15m"}


def test_missing_ltf_degrades_with_disclosure(tmp_path):
    class _IntradayDown(demo.DemoOHLCVTool):
        def run(self, symbol: str, period: str = "6mo", interval: str = "1d"):
            if interval != "1d":
                raise RuntimeError("intraday feed down")
            return super().run(symbol, period, interval)

    tools = _tools(tmp_path / "artifacts")
    tools.ohlcv = _IntradayDown(tools.ohlcv.artifacts_dir, "crypto")
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          tools, EventBus(), tmp_path)
    report = run.report
    assert all(s.entry_plan is None for s in report.signals)
    # the demo bear market emits SHORTs: their ladder failures disclose too
    assert any("entry refinement" in d or "no 15m bars" in d or "no 30m bars" in d
               for d in report.disclosures), report.disclosures
    assert report.citation_coverage == 1.0


def test_deepdive_entry_plan(tmp_path):
    from signaldesk.tools import demo_equities
    from signaldesk.workflows.deep_dive import DeepDiveRequest, DeepDiveToolSet, run_deep_dive

    d = tmp_path / "artifacts"
    tools = DeepDiveToolSet(
        quotes=demo.DemoQuotesTool(d, "equities"),
        ohlcv=demo.DemoOHLCVTool(d, "equities"),
        search=demo.DemoSearchTool(),
        fundamentals=demo_equities.DemoFundamentalsTool(d),
        edgar=demo_equities.DemoEdgarTool(d),
        earnings=demo_equities.DemoEarningsTool(d),
    )
    run = run_deep_dive(DeepDiveRequest(symbol="AAPL"), tools, EventBus(), tmp_path)
    r = run.report
    if r.signal is not None:  # demo AAPL clears the threshold
        p = r.signal.entry_plan
        assert p is not None
        assert p.mode in ("market", "pullback", "wait")
        assert p.stop < p.entry < p.tp1 < p.tp2
        for cid in p.citations:
            assert cid in r.citations
    assert r.citation_coverage == 1.0


def test_report_surfaces_cost_and_risk_floor(tmp_path):
    """P0: cost-in-R is disclosed, and the floor bounds the refined risk."""
    from signaldesk.report.render import to_markdown

    report = _scan(tmp_path)
    assert any("Cost model" in d for d in report.disclosures)
    md = to_markdown(report)
    assert "## Risk units & costs" in md
    assert "Break-even @TP1" in md
    for s in report.signals:
        assert s.cost_pct > 0
        assert 0.0 < s.cost_in_r <= 1 / 15 + 1e-6   # floored: cost stays a small part of R
        p = s.entry_plan
        if p is not None and p.mode in ("market", "pullback"):
            ratio = p.risk_refined / p.risk_daily
            assert 0 < ratio <= 1.0001, f"{s.symbol}: refined risk ratio {ratio}"
            assert p.cost_in_r <= 1 / 15 + 1e-6
            assert p.stop < p.entry < p.tp1 < p.tp2
