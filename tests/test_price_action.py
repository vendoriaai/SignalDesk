import pandas as pd
import pytest

from signaldesk.strategy.price_action import (
    analyse_price_action, detect_candle_patterns, find_support_resistance,
)


def frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_rejects_invalid_ohlc():
    with pytest.raises(ValueError, match="missing columns"):
        detect_candle_patterns(pd.DataFrame({"close": [1, 2, 3]}))


def test_detects_bullish_engulfing():
    data = frame([
        (10, 11, 8, 9),   # bearish
        (8.5, 12, 8, 11.5),  # bullish real body engulfs prior body
        (11.5, 12, 10, 11),
    ])
    found = detect_candle_patterns(data)
    assert any(p.name == "bullish_engulfing" and p.direction == "bullish" for p in found)


def test_detects_inside_bar():
    data = frame([
        (10, 12, 8, 11), (10.5, 11.5, 8.5, 10.8), (10.8, 13, 9, 12),
    ])
    found = detect_candle_patterns(data)
    assert any(p.name == "inside_bar" for p in found)


def test_support_resistance_levels_are_clustered():
    data = frame([
        (10, 12, 9, 11), (11, 13, 10, 12), (12, 14, 11, 13),
        (13, 13.5, 10, 11), (11, 12, 9, 10), (10, 12.1, 8.5, 11),
        (11, 13, 9, 12), (12, 14.1, 10, 13), (13, 13, 9, 10),
    ])
    levels = find_support_resistance(data, pivot_window=1, tolerance_pct=1.0)
    assert all(level.touches >= 1 for level in levels)
    assert all(level.kind in ("support", "resistance") for level in levels)


def test_analyse_returns_serializable_features():
    data = frame([
        (10, 11, 9, 10), (10, 12, 9, 11), (11, 13, 10, 12),
        (12, 14, 11, 13), (13, 14, 12, 12.5), (12.5, 13, 11, 12),
        (12, 13, 10, 11), (11, 12, 9, 10),
    ] * 5)
    result = analyse_price_action(data)
    assert result.trend in ("bullish", "bearish", "neutral")
    assert "patterns" in result.to_dict()
    assert set(result.latest_candle) >= {"open", "high", "low", "close", "close_location"}


def test_invalid_configuration_raises():
    data = frame([(1, 2, 0, 1), (1, 3, 1, 2), (2, 4, 1, 3)])
    with pytest.raises(ValueError):
        find_support_resistance(data, tolerance_pct=0)
