"""Cost model: per-symbol lookups, cost-in-R, break-even hit rate."""
import pytest

from signaldesk import costs


def test_symbol_override_beats_market_default():
    assert costs.round_trip_cost_pct("crypto", "BTCUSD") == 0.25
    assert costs.round_trip_cost_pct("crypto", "SUIUSD") == 0.40   # alt default
    assert costs.round_trip_cost_pct("forex", "EURUSD") == 0.02
    assert costs.round_trip_cost_pct("equities", "AAPL") == 0.05
    assert costs.round_trip_cost_pct("metals", "XAGUSD") == 0.05


def test_unknown_market_falls_back_conservatively():
    assert costs.round_trip_cost_pct("commodities", "WHATEVER") == costs.FALLBACK_COST_PCT
    assert costs.FALLBACK_COST_PCT > 0


def test_cost_in_r():
    assert costs.cost_in_r(0.5, 5.0) == pytest.approx(0.1)
    assert costs.cost_in_r(0.25, 0.16) == pytest.approx(1.5625)
    assert costs.cost_in_r(0.5, 0.0) == float("inf")  # degenerate risk is flagged


def test_breakeven_win_rate_matches_random_walk_baseline():
    assert costs.breakeven_win_rate(0.0, 2.0) == pytest.approx(1 / 3)
    assert costs.breakeven_win_rate(0.0, 3.0) == pytest.approx(0.25)
    # 0.3R of cost at a 2R target needs 43% not 33%
    assert costs.breakeven_win_rate(0.3, 2.0) == pytest.approx(1.3 / 3)


def test_carry_cost_is_crypto_only_and_scales_with_days():
    assert costs.carry_cost_pct("crypto", 0) == 0.0
    assert costs.carry_cost_pct("forex", 30) == 0.0
    assert costs.carry_cost_pct("crypto", 365) == pytest.approx(costs.CRYPTO_FUNDING_ANNUAL_PCT)
    assert costs.carry_cost_pct("crypto", 7) == pytest.approx(costs.CRYPTO_FUNDING_ANNUAL_PCT * 7 / 365)
