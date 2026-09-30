"""Regime gates (roadmap item 32, trial T2): the engine refuses a side when
trend, volatility extremes or the candidate's own extension say no. The short
side runs the same gates mirrored."""
import pytest

from signaldesk.strategy import scoring
from signaldesk.strategy.scoring import RegimeVerdict, regime_gate, regime_gate_short

nan = float("nan")


def test_parabolic_7d_gain_is_gated():
    v = regime_gate({"close": 2.0, "ret_7d": 251.0, "vol_ann": 80.0,
                     "sma20": 1.5, "sma200": 1.0})
    assert "parabolic-extension" in v.reason and "+251%" in v.reason
    assert v.column == "ret_7d" and v.value == 251.0


def test_volatility_extreme_is_gated():
    v = regime_gate({"close": 1.0, "ret_7d": 5.0, "vol_ann": 220.0,
                     "sma20": 0.95, "sma200": 0.8})
    assert "extremes gate" in v.reason
    assert v.column == "vol_ann" and v.value == 220.0


def test_price_extended_over_sma20_is_gated():
    v = regime_gate({"close": 1.40, "ret_7d": 10.0, "vol_ann": 60.0,
                     "sma20": 1.0, "sma200": 0.9})     # 40% over SMA20
    assert "extension gate" in v.reason
    assert v.column == "sma20_dist_pct" and v.value == pytest.approx(40.0)


def test_below_200d_sma_is_gated():
    v = regime_gate({"close": 0.9, "ret_7d": 2.0, "vol_ann": 50.0,
                     "sma20": 0.95, "sma200": 1.1})
    assert "200d SMA" in v.reason
    assert v.column == "close_over_sma200" and v.value == pytest.approx(0.9 / 1.1)


def test_healthy_setup_passes():
    v = regime_gate({"close": 1.05, "ret_7d": 3.0, "vol_ann": 55.0,
                     "sma20": 1.0, "sma200": 0.9})
    assert v.reason == ""


def test_nan_or_missing_values_never_gate():
    nan = float("nan")
    v = regime_gate({"close": 1.05, "ret_7d": nan, "vol_ann": None,
                     "sma20": nan, "sma200": nan})
    assert v.reason == ""          # cannot judge -> allowed, disclosed elsewhere
    assert regime_gate({}).reason == ""


def test_priority_vol_first_then_extension():
    v = regime_gate({"close": 2.0, "ret_7d": 90.0, "vol_ann": 300.0,
                     "sma20": 1.0, "sma200": 0.5})
    assert v.column == "vol_ann"   # most extreme classification wins


def test_market_gate_btc_below_200d():
    assert scoring.market_regime_below_trend({"close": 80_000.0, "sma200": 90_000.0})
    assert not scoring.market_regime_below_trend({"close": 95_000.0, "sma200": 90_000.0})
    assert not scoring.market_regime_below_trend({"close": 80_000.0, "sma200": nan})


# short-side mirror (declared policy, same thresholds) ---------------------------

def test_short_mirror_falling_knife_is_gated():
    v = regime_gate_short({"close": 0.5, "ret_7d": -62.0, "vol_ann": 60.0,
                           "sma20": 0.8, "sma200": 0.9})
    assert "falling-knife" in v.reason
    assert v.column == "ret_7d" and v.value == -62.0


def test_short_mirror_vol_extreme_is_gated():
    v = regime_gate_short({"close": 1.0, "ret_7d": -5.0, "vol_ann": 220.0,
                           "sma20": 1.1, "sma200": 0.8})
    assert "extremes gate" in v.reason and "squeeze" in v.reason
    assert v.column == "vol_ann" and v.value == 220.0


def test_short_mirror_extended_below_sma20_is_gated():
    v = regime_gate_short({"close": 0.60, "ret_7d": -5.0, "vol_ann": 60.0,
                           "sma20": 1.0, "sma200": 0.9})     # 40% below SMA20
    assert "extension gate" in v.reason
    assert v.column == "sma20_dist_pct" and v.value == pytest.approx(-40.0)


def test_short_mirror_above_200d_sma_is_gated():
    v = regime_gate_short({"close": 1.1, "ret_7d": 2.0, "vol_ann": 50.0,
                           "sma20": 0.95, "sma200": 1.0})
    assert "200d SMA" in v.reason and "short" in v.reason
    assert v.column == "close_over_sma200" and v.value == pytest.approx(1.1)


def test_short_mirror_healthy_downtrend_passes():
    v = regime_gate_short({"close": 0.95, "ret_7d": -3.0, "vol_ann": 55.0,
                           "sma20": 1.0, "sma200": 1.1})
    assert v.reason == ""


def test_short_mirror_nan_or_missing_never_gates():
    v = regime_gate_short({"close": 0.95, "ret_7d": nan, "vol_ann": None,
                           "sma20": nan, "sma200": nan})
    assert v.reason == ""
    assert regime_gate_short({}).reason == ""


def test_short_market_gate_btc_above_200d():
    assert scoring.market_regime_above_trend({"close": 95_000.0, "sma200": 90_000.0})
    assert not scoring.market_regime_above_trend({"close": 80_000.0, "sma200": 90_000.0})
    assert not scoring.market_regime_above_trend({"close": 95_000.0, "sma200": nan})


# workflow wiring ----------------------------------------------------------------

def _demo_tools(d):
    from signaldesk.tools import demo
    from signaldesk.workflows.market_scan import ToolSet

    return ToolSet(
        movers=demo.DemoMoversTool(d, "crypto"), quotes=demo.DemoQuotesTool(d, "crypto"),
        ohlcv=demo.DemoOHLCVTool(d, "crypto"), search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(d), altseason=demo.DemoAltSeasonTool(d),
    )


def test_demo_scan_passes_the_gates_and_discloses_policy(tmp_path):
    from signaldesk.agent.events import EventBus
    from signaldesk.workflows.market_scan import MarketScanRequest, run_market_scan

    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          _demo_tools(tmp_path / "artifacts"), EventBus(), tmp_path)
    r = run.report
    # the demo market is a mild bear: BTC below its 200d SMA stands the long
    # book down, so every emitted signal must be a SHORT and every refusal
    # (either side) lands in the avoid list with the policy disclosure
    assert r.signals, "bear demo must emit SHORT signals"
    assert all(s.direction == "SHORT" for s in r.signals)
    assert any("BTC below its 200d SMA" in a.reason for a in r.avoid)
    assert any("Regime gates (item 32" in d for d in r.disclosures)


def test_gated_symbols_land_in_avoid_with_cited_values(tmp_path, monkeypatch):
    from signaldesk.agent.events import EventBus
    from signaldesk.workflows.market_scan import MarketScanRequest, run_market_scan

    verdict = RegimeVerdict(reason="gate test reason", value=99.0,
                            column="ret_7d", formula="test")
    monkeypatch.setattr(scoring, "regime_gate", lambda feat: verdict)
    monkeypatch.setattr(scoring, "regime_gate_short", lambda feat: verdict)
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          _demo_tools(tmp_path / "artifacts"), EventBus(), tmp_path)
    r = run.report
    assert r.signals == []
    assert r.avoid and all("gate test reason" in a.reason for a in r.avoid)
    assert any("regime gate" in c.label and "ret_7d" in c.label
               for c in r.citations.values())


def test_market_gate_caps_the_whole_crypto_book(tmp_path, monkeypatch):
    from signaldesk.agent.events import EventBus
    from signaldesk.workflows.market_scan import MarketScanRequest, run_market_scan

    monkeypatch.setattr(scoring, "market_regime_below_trend", lambda feat: True)
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          _demo_tools(tmp_path / "artifacts"), EventBus(), tmp_path)
    r = run.report
    # the long book stands down book-wide; the short book stays per-symbol, so
    # the bear demo still emits SHORTs and every LONG refusal cites the gate
    assert all(s.direction == "SHORT" for s in r.signals)
    assert all("BTC below its 200d SMA" in a.reason
               for a in r.avoid if "LONG refused" in a.reason)
    assert any("Regime gate active" in d for d in r.disclosures)


def test_market_gate_caps_the_whole_crypto_short_book(tmp_path, monkeypatch):
    from signaldesk.agent.events import EventBus
    from signaldesk.workflows.market_scan import MarketScanRequest, run_market_scan

    monkeypatch.setattr(scoring, "market_regime_above_trend", lambda feat: True)
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          _demo_tools(tmp_path / "artifacts"), EventBus(), tmp_path)
    r = run.report
    # longs still flow in the demo uptrend; the short book stands down, disclosed
    assert all(s.direction != "SHORT" for s in r.signals)
    assert any("short book stands down" in d for d in r.disclosures)
    assert any("SHORT refused" in a.reason for a in r.avoid)
