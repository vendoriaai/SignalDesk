"""Technical indicator primitives (workflows.md WF-1 Phase 4).

Pure pandas/numpy so the sandbox can import them with a minimal whitelist.
Wilder smoothing is used for RSI and ATR, matching the reference trace.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def annualized_volatility(close: pd.Series, window: int = 20, periods_per_year: int = 365) -> pd.Series:
    return close.pct_change().rolling(window).std(ddof=1) * np.sqrt(periods_per_year) * 100


def returns_pct(close: pd.Series, windows: tuple[int, ...] = (3, 7, 30)) -> dict[str, pd.Series]:
    return {f"ret_{w}d": close.pct_change(w) * 100 for w in windows}


def swing_high_low(high: pd.Series, low: pd.Series, window: int = 20):
    return high.rolling(window).max(), low.rolling(window).min()


def _last(s: pd.Series) -> float:
    return float(s.iloc[-1]) if len(s) and pd.notna(s.iloc[-1]) else float("nan")


def _prev(s: pd.Series) -> float:
    return float(s.iloc[-2]) if len(s) > 1 and pd.notna(s.iloc[-2]) else float("nan")


def compute_features(df: pd.DataFrame) -> dict:
    """Latest-bar feature row for one symbol's daily OHLCV frame.

    Expects columns date/open/high/low/close/volume sorted ascending.
    Booleans mirror workflows.md Phase 4 step 9.
    """
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    rsi14 = rsi_wilder(close)
    sma20, sma50 = sma(close, 20), sma(close, 50)
    sma200 = sma(close, 200)   # NaN below 200 bars — 200d regime gate unevaluated
    ema9, ema21 = ema(close, 9), ema(close, 21)
    macd_line, macd_sig, macd_hist = macd(close)
    atr14 = atr_wilder(high, low, close)
    sh, sl = swing_high_low(high, low)
    vol_avg30 = vol.rolling(30).mean()

    c = _last(close)
    feats: dict[str, float | bool] = {
        "as_of": str(pd.to_datetime(df["date"].iloc[-1]).date()),
        "close": c,
        "rsi14": _last(rsi14),
        "sma20": _last(sma20),
        "sma50": _last(sma50),
        "sma200": _last(sma200),
        "ema9": _last(ema9),
        "ema21": _last(ema21),
        "macd": _last(macd_line),
        "macd_signal": _last(macd_sig),
        "macd_hist": _last(macd_hist),
        "macd_hist_prev": _prev(macd_hist),
        "atr14": _last(atr14),
        "vol_ann": _last(annualized_volatility(close)),
        "swing_high_20": _last(sh),
        "swing_low_20": _last(sl),
        "volume": _last(vol),
        "volume_avg30": _last(vol_avg30),
    }
    for name, series in returns_pct(close).items():
        feats[name] = _last(series)

    feats.update(
        above_sma20=bool(c > feats["sma20"]),
        above_sma50=bool(c > feats["sma50"]),
        ema9_above_ema21=bool(feats["ema9"] > feats["ema21"]),
        macd_above_signal=bool(feats["macd_hist"] > 0),
        macd_hist_rising=bool(feats["macd_hist"] > feats["macd_hist_prev"]),
        macd_bullish_cross=bool(
            feats["macd_hist"] > 0 >= feats["macd_hist_prev"] and not np.isnan(feats["macd_hist_prev"])
        ),
    )
    return feats
