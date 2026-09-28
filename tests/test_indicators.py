import numpy as np
import pandas as pd

from signaldesk.analysis.indicators import (
    atr_wilder,
    compute_features,
    macd,
    rsi_wilder,
)


def test_rsi_all_up_is_100():
    close = pd.Series(np.arange(1.0, 60.0))  # strictly increasing
    assert rsi_wilder(close).iloc[-1] == 100.0


def test_rsi_all_down_near_zero():
    close = pd.Series(np.arange(60.0, 1.0, -1.0))  # strictly decreasing
    assert rsi_wilder(close).iloc[-1] < 5.0


def test_rsi_bounded_flat_is_50ish():
    close = pd.Series([10.0 + (0.1 if i % 2 else -0.1) for i in range(80)])
    rsi = rsi_wilder(close).iloc[-1]
    assert 40.0 < rsi < 60.0  # symmetric up/down chops around neutral


def test_macd_constant_series_is_zero():
    line, sig, hist = macd(pd.Series([100.0] * 60))
    assert abs(line.iloc[-1]) < 1e-9
    assert abs(hist.iloc[-1]) < 1e-9


def test_atr_steps_up_after_gap():
    # 20 calm bars of range 1, then a gap that triples true range
    closes = np.concatenate([np.full(20, 100.0), np.array([110.0])])
    high = pd.Series(closes + 0.5)
    low = pd.Series(closes - 0.5)
    close = pd.Series(closes)
    atr = atr_wilder(high, low, close)
    assert atr.iloc[-1] > 1.0  # pick up the gap |H - Cprev| = 10.5


def test_compute_features_keys_and_bools():
    n = 120
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    up = np.linspace(100, 160, n) * (1 + 0.01 * np.sin(np.arange(n)))
    df = pd.DataFrame(
        {"date": idx, "open": up, "high": up * 1.01, "low": up * 0.99,
         "close": up, "volume": np.full(n, 1e6)}
    )
    feats = compute_features(df)
    for key in ("rsi14", "sma20", "sma50", "ema9", "ema21", "macd_hist",
                "atr14", "vol_ann", "ret_3d", "ret_7d", "ret_30d",
                "swing_high_20", "swing_low_20", "volume_avg30"):
        assert key in feats, key
    assert feats["above_sma20"] is True
    assert feats["above_sma50"] is True
    assert feats["ema9_above_ema21"] is True