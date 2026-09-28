"""Risk-unit policy: the stop floor that keeps cost small relative to R.

Background (workflows.md Phase 7/7.5): the intraday refinement used to recompute
the stop from an intraday bar-count swing low, collapsing R from ~5% of price to
~0.16% — more than 1R of round-trip cost. The floor below is the fix.
"""
import pytest

from signaldesk import costs
from signaldesk.strategy import scoring


def test_daily_plan_floors_a_too_tight_swing_low():
    """A swing low 1% below entry would be R = 1 < 0.75 x ATR(4): widen it."""
    plan = scoring.build_trade_plan(entry=100.0, atr=4.0, swing_low=99.0)
    assert plan.floored
    assert plan.stop == pytest.approx(97.0)          # 0.75 x 4 = 3
    assert plan.tp1 == pytest.approx(106.0)
    assert plan.tp2 == pytest.approx(109.0)


def test_daily_plan_cost_floor_wins_for_expensive_instruments():
    """15 x 0.40% round trip = 6% of entry, wider than 0.75 x ATR(2)."""
    plan = scoring.build_trade_plan(entry=100.0, atr=2.0, swing_low=98.0, cost_pct=0.40)
    assert plan.floored
    assert plan.stop == pytest.approx(94.0)
    risk_pct = (plan.entry - plan.stop) / plan.entry * 100.0
    assert costs.cost_in_r(0.40, risk_pct) == pytest.approx(0.40 / 6)


def test_daily_plan_untouched_when_structure_stop_is_already_wide():
    plan = scoring.build_trade_plan(entry=100.0, atr=4.0, swing_low=90.0, cost_pct=0.25)
    assert not plan.floored
    assert plan.stop == pytest.approx(94.0)


def _ltf(**over):
    base = dict(close=100.0, ema9=99.8, ema21=99.5, rsi14=60.0, atr14=0.12,
                macd_hist=0.1, macd_hist_prev=0.05, swing_low_20=99.9)
    base.update(over)
    return base


def test_refined_risk_keeps_cost_a_small_fraction_of_r():
    daily = scoring.build_trade_plan(entry=100.0, atr=4.0, swing_low=90.0)  # R = 6
    plan = scoring.refine_entry_plan(daily, {"15m": _ltf()}, atr_daily=4.0, cost_pct=0.25)
    assert plan.mode == "market"
    assert plan.floored
    # floor = max(0.75 x 4 = 3.0, 15 x 0.25% x 100 = 3.75) = 3.75
    assert plan.risk_refined == pytest.approx(3.75)
    assert plan.risk_refined < plan.risk_daily
    assert plan.stop < plan.entry < plan.tp1 < plan.tp2
    assert plan.tp1 == pytest.approx(plan.entry + 2 * plan.risk_refined)
    cost_r = 0.25 / (plan.risk_refined / plan.entry * 100.0)
    assert cost_r <= 1 / scoring.MIN_RISK_COST_MULT + 1e-9


def test_pullback_entry_also_respects_the_floor():
    daily = scoring.build_trade_plan(entry=100.0, atr=4.0, swing_low=90.0)
    ltf = {"15m": _ltf(rsi14=72.0, ema21=97.0, atr14=0.2, swing_low_20=96.9)}
    plan = scoring.refine_entry_plan(daily, ltf, atr_daily=4.0, cost_pct=0.25)
    assert plan.mode == "pullback"
    assert plan.entry == pytest.approx(97.0)
    # cost term scales with the pullback entry: 15 x 0.25% x 97 = 3.6375
    assert plan.risk_refined == pytest.approx(3.6375)
    assert plan.tp1 == pytest.approx(97.0 + 2 * 3.6375)
    assert plan.floored


def test_refinement_still_can_tighten_within_the_floor():
    """R = 1.5 x ATR(1d) daily -> the intraday read may tighten it to 0.75 x ATR(1d)."""
    daily = scoring.build_trade_plan(entry=100.0, atr=4.0, swing_low=90.0)  # R = 6
    plan = scoring.refine_entry_plan(daily, {"15m": _ltf()}, atr_daily=4.0, cost_pct=0.01)
    assert plan.risk_refined == pytest.approx(3.0)   # 0.75 x ATR, not the 0.18 risk suggested by 15m ATR
    assert plan.risk_refined == pytest.approx(0.5 * plan.risk_daily)


def test_without_floor_inputs_behaviour_is_unchanged():
    """Direct callers/tests that pass no ATR or cost keep the pre-floor rules."""
    daily = scoring.build_trade_plan(entry=100.0, atr=2.0, swing_low=90.0)  # R = 3
    plan = scoring.refine_entry_plan(daily, {"15m": _ltf(atr14=0.4),
                                            "30m": _ltf(atr14=0.5)})
    assert plan.mode == "market"
    assert not plan.floored
    assert plan.risk_refined == pytest.approx(0.75)  # 1.5 x ATR(30m)


def test_min_risk_distance_inputs():
    assert scoring.min_risk_distance(100.0, 4.0, None) == pytest.approx(3.0)
    assert scoring.min_risk_distance(100.0, None, 0.4) == pytest.approx(6.0)
    assert scoring.min_risk_distance(100.0, 4.0, 0.4) == pytest.approx(6.0)
    assert scoring.min_risk_distance(100.0, None, None) == 0.0
    assert scoring.min_risk_distance(100.0, float("nan"), 0.0) == 0.0


def test_weights_fingerprint_is_stable_and_short():
    a = scoring.weights_fingerprint()
    assert a == scoring.weights_fingerprint()
    assert len(a) == 16 and all(c in "0123456789abcdef" for c in a)
