import pytest

from signaldesk.strategy.scoring import (
    RSI_OVEREXTENDED,
    STOP_FLOOR_MULT,
    SymbolFeatures,
    build_trade_plan,
    refine_entry_plan,
    score_symbol,
)


def bull(**over):
    base = dict(
        close=100.0, rsi14=62.0, atr14=4.0, swing_low_20=90.0,
        above_sma20=True, above_sma50=True, ema9_above_ema21=True,
        macd_hist=0.5, macd_hist_prev=0.2, volume=2.0e8, volume_avg30=1.5e8,
    )
    base.update(over)
    return SymbolFeatures(**base)


def test_full_bullish_greed_scores_100():
    s = score_symbol(bull(), fear_greed=60.0)
    assert s.total == 100.0
    assert not s.overextended and not s.hold_capped


def test_no_signal_traits_scores_low():
    s = score_symbol(
        bull(above_sma20=False, above_sma50=False, ema9_above_ema21=False,
             macd_hist=-0.5, macd_hist_prev=0.2, rsi14=30.0,
             volume_avg30=3.0e8),
        fear_greed=20.0,
    )
    assert s.total < 20.0


def test_rsi_75_is_overextended_and_excluded():
    s = score_symbol(bull(rsi14=75.2), fear_greed=60.0)
    assert s.overextended  # rule R3: never emitted as LONG


def test_rsi_borderline_half_credit():
    s = score_symbol(bull(rsi14=47.0), fear_greed=None)
    assert s.rsi_regime == 7.5
    s = score_symbol(bull(rsi14=72.0), fear_greed=None)
    assert s.rsi_regime == 7.5


def test_extreme_greed_caps_at_hold():
    s = score_symbol(bull(), fear_greed=85.0)
    assert s.hold_capped
    assert s.sentiment == 0.0


def test_trade_plan_geometry():
    plan = build_trade_plan(entry=100.0, atr=4.0, swing_low=90.0)
    # stop candidates: 94.0 (1.5*ATR) vs 90.0 (swing) -> closest is 94.0
    assert plan.stop == 94.0
    assert plan.tp1 == 112.0  # entry + 2R
    assert plan.tp2 == 118.0  # entry + 3R


def test_trade_plan_widens_a_swing_low_inside_the_noise_band():
    """Post-floor contract: a 2-wide swing low with ATR 10 is widened to 0.75 x ATR."""
    plan = build_trade_plan(entry=100.0, atr=10.0, swing_low=98.0)
    assert plan.floored
    assert plan.stop == pytest.approx(92.5)


def test_trade_plan_pick_swing_when_closer_and_wide_enough():
    """The 'closest of' rule still holds when the swing low clears the floor."""
    plan = build_trade_plan(entry=100.0, atr=10.0, swing_low=92.5)
    assert not plan.floored
    assert plan.stop == pytest.approx(92.5)
    plan = build_trade_plan(entry=100.0, atr=10.0, swing_low=96.0)
    assert plan.stop == pytest.approx(92.5)  # floored: 4 < 7.5


def test_overextended_constant():
    assert RSI_OVEREXTENDED == 75.0


# --- Phase 7.5 intraday entry refinement -----------------------------------------

def ltf_feat(**over):
    base = dict(close=100.0, ema9=99.8, ema21=99.5, rsi14=60.0, atr14=0.4,
                macd_hist=0.1, macd_hist_prev=0.05, swing_low_20=99.2)
    base.update(over)
    return base


def test_refine_market_mode_tightens_stop():
    daily = build_trade_plan(entry=100.0, atr=2.0, swing_low=90.0)  # stop 97, risk 3
    plan = refine_entry_plan(daily, {"15m": ltf_feat(), "30m": ltf_feat(atr14=0.5, swing_low_20=99.0)})
    assert plan.mode == "market"
    assert daily.stop < plan.stop < plan.entry  # tightened but sane
    assert plan.stop <= plan.entry - STOP_FLOOR_MULT * 0.5  # floor caps tightness
    assert plan.risk_refined < plan.risk_daily
    assert plan.stop < plan.entry < plan.tp1 < plan.tp2
    assert plan.tp1 == plan.entry + 2 * plan.risk_refined
    assert plan.tp2 == plan.entry + 3 * plan.risk_refined


def test_refine_pullback_when_overextended():
    daily = build_trade_plan(entry=100.0, atr=2.0, swing_low=90.0)
    ltf = {
        "15m": ltf_feat(rsi14=73.0, ema21=98.8, atr14=0.3, swing_low_20=98.5),
        "5m": ltf_feat(rsi14=75.0),
    }
    plan = refine_entry_plan(daily, ltf)
    assert plan.mode == "pullback"
    assert plan.entry == 98.8  # the 15m EMA21 zone
    assert plan.stop < plan.entry < plan.tp1 < plan.tp2
    assert plan.risk_refined > 0


def test_refine_wait_when_ltf_bearish():
    daily = build_trade_plan(entry=100.0, atr=2.0, swing_low=90.0)
    ltf = {"15m": ltf_feat(close=99.0, ema21=99.5, macd_hist=-0.1, macd_hist_prev=-0.05)}
    plan = refine_entry_plan(daily, ltf)
    assert plan.mode == "wait"
    assert (plan.entry, plan.stop, plan.tp1, plan.tp2) == \
        (daily.entry, daily.stop, daily.tp1, daily.tp2)
    assert plan.risk_refined == plan.risk_daily


def test_refine_without_ltf_keeps_daily():
    daily = build_trade_plan(entry=100.0, atr=2.0, swing_low=90.0)
    plan = refine_entry_plan(daily, {})
    assert plan.mode == "wait"
    assert (plan.entry, plan.stop, plan.tp1, plan.tp2) == \
        (daily.entry, daily.stop, daily.tp1, daily.tp2)


def test_refine_never_degenerate_stop():
    daily = build_trade_plan(entry=100.0, atr=2.0, swing_low=90.0)
    # swing low extremely close to the EMA21 entry: floor must keep risk > 0
    ltf = {"15m": ltf_feat(rsi14=70.0, ema21=98.8, atr14=0.3, swing_low_20=98.79)}
    plan = refine_entry_plan(daily, ltf)
    assert plan.stop < plan.entry
    assert plan.risk_refined > 0