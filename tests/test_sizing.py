"""Position sizing (roadmap item 30): inverse-volatility risk + cluster cap."""
import math

from signaldesk.strategy import sizing


def test_median_volatility_signal_gets_the_base_risk():
    vols = {"LOWVOL": 2.5, "MEDVOL": 5.0, "HIVOL": 10.0}
    sizes = sizing.size_signals(vols)
    assert sizes["MEDVOL"].risk_pct_account == 0.75        # median -> base
    assert sizes["LOWVOL"].risk_pct_account > 0.75         # less volatile -> more risk
    assert sizes["HIVOL"].risk_pct_account < 0.75


def test_risk_stays_inside_the_declared_band():
    vols = {f"S{i}": v for i, v in enumerate([0.5, 1.0, 2.0, 5.0, 10.0, 25.0])}
    # cap disabled: the per-trade band holds before the cluster cap kicks in
    sizes = sizing.size_signals(vols, cluster_cap_risk_pct=100.0)
    for s in sizes.values():
        assert sizing.MIN_RISK_PCT <= s.risk_pct_account <= sizing.MAX_RISK_PCT


def test_cluster_cap_overrides_the_per_trade_floor():
    vols = {f"S{i}": v for i, v in enumerate([0.5, 1.0, 2.0, 5.0, 10.0, 25.0])}
    sizes = sizing.size_signals(vols)                      # cap active: book must fit 3%
    assert all(s.capped for s in sizes.values())
    assert math.isclose(sum(s.risk_pct_account for s in sizes.values()),
                        sizing.CLUSTER_CAP_RISK_PCT, rel_tol=1e-3)


def test_notional_is_risk_over_stop_distance():
    sizes = sizing.size_signals({"SOLO": 5.0})             # one signal: risk = base
    assert sizes["SOLO"].risk_pct_account == 0.75
    assert sizes["SOLO"].notional_pct_account == 15.0      # 0.75% / 5% * 100
    assert sizes["SOLO"].weight == 1.0


def test_cluster_cap_scales_the_book_down():
    vols = {f"S{i}": 5.0 for i in range(10)}               # 10 x 0.75% = 7.5% total
    sizes = sizing.size_signals(vols)
    total = sum(s.risk_pct_account for s in sizes.values())
    assert math.isclose(total, sizing.CLUSTER_CAP_RISK_PCT, rel_tol=1e-3)
    assert all(s.capped for s in sizes.values())
    assert all(s.risk_pct_account < sizing.BASE_RISK_PCT for s in sizes.values())
    assert math.isclose(sum(s.weight for s in sizes.values()), 1.0, abs_tol=1e-3)


def test_no_cap_when_the_book_is_small():
    sizes = sizing.size_signals({"A": 5.0, "B": 5.0})      # 2 x 0.75% = 1.5% < 3%
    assert not any(s.capped for s in sizes.values())
    assert sum(s.risk_pct_account for s in sizes.values()) == 1.5


def test_invalid_and_missing_vols_are_skipped():
    assert sizing.size_signals({}) == {}
    sizes = sizing.size_signals({"BAD": 0.0, "NEG": -2.0, "OK": 5.0})
    assert set(sizes) == {"OK"}


def test_demo_scan_attaches_sizing_and_discloses_it(tmp_path):
    from signaldesk.agent.events import EventBus
    from signaldesk.tools import demo
    from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

    d = tmp_path / "artifacts"
    tools = ToolSet(
        movers=demo.DemoMoversTool(d, "crypto"), quotes=demo.DemoQuotesTool(d, "crypto"),
        ohlcv=demo.DemoOHLCVTool(d, "crypto"), search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(d), altseason=demo.DemoAltSeasonTool(d),
    )
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          tools, EventBus(), tmp_path)
    report = run.report
    assert report.signals, "demo scan must produce signals"
    for s in report.signals:
        assert s.sizing is not None
        assert 0 < s.sizing.risk_pct_account <= sizing.MAX_RISK_PCT
        assert s.sizing.notional_pct_account > 0
    assert any("Sizing model" in d for d in report.disclosures)
    assert "Account risk" in run.report_md                 # rendered into the report
