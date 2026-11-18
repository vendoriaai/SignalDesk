import pandas as pd
import pytest

from signaldesk.strategy.smc import analyse_smc, find_swings, timeframe_confluence


def bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_requires_valid_ohlc():
    with pytest.raises(ValueError, match="missing columns"):
        analyse_smc(pd.DataFrame({"close": [1, 2, 3, 4, 5]}))


def test_swings_are_confirmed_not_future_leaking():
    frame = bars([
        (10, 11, 9, 10), (10, 12, 9, 11), (11, 15, 10, 14),
        (11, 13, 10, 11), (11, 12, 9, 10), (10, 11, 8, 9),
    ])
    swings = find_swings(frame, left=1, right=1)
    assert any(s.kind == "high" and s.index == 2 and s.price == 15 for s in swings)


def test_fvg_detected_and_result_serializes():
    frame = bars([
        (10, 11, 9, 10), (10, 12, 10, 11), (12, 15, 12, 14),
        (14, 16, 13, 15), (15, 17, 14, 16), (16, 18, 15, 17),
    ])
    result = analyse_smc(frame, left=1, right=1, atr_period=2)
    assert any(g.direction == "bullish" and g.lower == 11 for g in result.fvgs)
    assert "fair_value_gaps" in result.to_dict()


def test_timeframe_confluence_mixed_when_directions_disagree():
    from signaldesk.strategy.smc import SMCResult, StructureEvent
    bullish = SMCResult([], [StructureEvent(1, "t1", "bullish", "BOS", 1.0, 0)], [], [], [], None)
    bearish = SMCResult([], [StructureEvent(1, "t2", "bearish", "MSS", 2.0, 0)], [], [], [], None)
    assert timeframe_confluence({"1h": bullish, "15m": bearish})["alignment"] == "mixed"
