"""Deterministic price-action features and candle-pattern detection.

Works on chronological OHLCV bars and deliberately does not issue trades.
Patterns are descriptive evidence; callers must combine them with market
structure, location, timeframe context and realistic risk/cost checks.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import pandas as pd

Direction = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True)
class CandlePattern:
    index: int
    timestamp: str
    name: str
    direction: Direction
    strength: float
    description: str


@dataclass(frozen=True)
class PriceLevel:
    price: float
    kind: Literal["support", "resistance"]
    touches: int
    first_index: int
    last_index: int


@dataclass
class PriceActionResult:
    patterns: list[CandlePattern]
    levels: list[PriceLevel]
    trend: Direction
    trend_strength: float
    latest_candle: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "patterns": [asdict(x) for x in self.patterns],
            "levels": [asdict(x) for x in self.levels],
            "trend": self.trend,
            "trend_strength": self.trend_strength,
            "latest_candle": self.latest_candle,
        }


def _prepare(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"open", "high", "low", "close"}
    frame = bars.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"OHLC frame missing columns: {sorted(missing)}")
    for col in required:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=list(required)).reset_index(drop=True)
    if len(frame) < 3:
        raise ValueError("at least three valid OHLC bars are required")
    invalid = (
        (frame.high < frame.low)
        | (frame.high < frame[["open", "close"]].max(axis=1))
        | (frame.low > frame[["open", "close"]].min(axis=1))
    )
    if invalid.any():
        raise ValueError("invalid OHLC candle")
    return frame


def _timestamp(frame: pd.DataFrame, i: int) -> str:
    for col in ("date", "datetime", "timestamp"):
        if col in frame.columns:
            return str(frame.iloc[i][col])
    return str(i)


def _candle_parts(frame: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=frame.index)
    out["range"] = (frame.high - frame.low).clip(lower=1e-12)
    out["body"] = (frame.close - frame.open).abs()
    out["body_low"] = frame[["open", "close"]].min(axis=1)
    out["body_high"] = frame[["open", "close"]].max(axis=1)
    out["upper_wick"] = frame.high - out.body_high
    out["lower_wick"] = out.body_low - frame.low
    out["body_ratio"] = out.body / out["range"]
    out["close_location"] = (frame.close - frame.low) / out["range"]
    return out


def detect_candle_patterns(bars: pd.DataFrame, *, lookback: int = 80) -> list[CandlePattern]:
    """Detect common patterns on closed candles; output is newest-first chronological."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    df = _prepare(bars)
    p = _candle_parts(df)
    start = max(1, len(df) - lookback)
    patterns: list[CandlePattern] = []

    def add(i: int, name: str, direction: Direction, strength: float, desc: str) -> None:
        patterns.append(CandlePattern(i, _timestamp(df, i), name, direction,
                                      round(float(np.clip(strength, 0, 1)), 3), desc))

    for i in range(start, len(df)):
        o, h, l, c = (float(df.iloc[i][x]) for x in ("open", "high", "low", "close"))
        body = float(p.iloc[i].body)
        rng = float(p.iloc[i]["range"])
        upper = float(p.iloc[i].upper_wick)
        lower = float(p.iloc[i].lower_wick)
        body_ratio = float(p.iloc[i].body_ratio)
        close_loc = float(p.iloc[i].close_location)
        bullish = c > o
        bearish = c < o

        # Doji: small real body; neutral by itself, not a reversal prediction.
        if body_ratio <= 0.10:
            add(i, "doji", "neutral", 1 - body_ratio / 0.10,
                "Small real body: indecision; direction needs surrounding context.")

        # Pin bars require a dominant rejection wick and close away from it.
        if lower >= max(body * 2.0, rng * 0.55) and close_loc >= 0.65:
            add(i, "bullish_pin_bar", "bullish", min(1, lower / rng),
                "Long lower-wick rejection; stronger near established support or swept lows.")
        if upper >= max(body * 2.0, rng * 0.55) and close_loc <= 0.35:
            add(i, "bearish_pin_bar", "bearish", min(1, upper / rng),
                "Long upper-wick rejection; stronger near established resistance or swept highs.")

        prev = df.iloc[i - 1]
        prev_body_low = min(float(prev.open), float(prev.close))
        prev_body_high = max(float(prev.open), float(prev.close))
        if bullish and float(prev.close) < float(prev.open):
            if min(o, c) <= prev_body_low and max(o, c) >= prev_body_high and body > 0:
                add(i, "bullish_engulfing", "bullish", min(1, body / max(abs(float(prev.close)-float(prev.open)), 1e-12)),
                    "Bullish real body engulfs the prior bearish real body.")
        if bearish and float(prev.close) > float(prev.open):
            if min(o, c) <= prev_body_low and max(o, c) >= prev_body_high and body > 0:
                add(i, "bearish_engulfing", "bearish", min(1, body / max(abs(float(prev.close)-float(prev.open)), 1e-12)),
                    "Bearish real body engulfs the prior bullish real body.")

        # Harami: current real body is contained by previous real body.
        if body > 0 and prev_body_high > prev_body_low:
            contained = min(o, c) >= prev_body_low and max(o, c) <= prev_body_high
            if contained and float(prev.close) < float(prev.open) and bullish:
                add(i, "bullish_harami", "bullish", 0.55,
                    "Bullish body is contained within the prior bearish body; potential pause/reversal.")
            if contained and float(prev.close) > float(prev.open) and bearish:
                add(i, "bearish_harami", "bearish", 0.55,
                    "Bearish body is contained within the prior bullish body; potential pause/reversal.")

        # Inside/outside bars use the full candle range.
        if h < float(prev.high) and l > float(prev.low):
            add(i, "inside_bar", "neutral", 0.6,
                "Range contraction inside prior candle; wait for a confirmed breakout.")
        if h > float(prev.high) and l < float(prev.low):
            direction: Direction = "bullish" if close_loc >= 0.7 else (
                "bearish" if close_loc <= 0.3 else "neutral")
            add(i, "outside_bar", direction, 0.65,
                "Current candle exceeds both prior extremes; close location gives directional bias.")

        # Strong close / displacement candle, normalized to its own range.
        if body_ratio >= 0.75 and close_loc >= 0.85:
            add(i, "bullish_displacement", "bullish", body_ratio,
                "Large bullish body closing near its high; check whether it breaks structure.")
        if body_ratio >= 0.75 and close_loc <= 0.15:
            add(i, "bearish_displacement", "bearish", body_ratio,
                "Large bearish body closing near its low; check whether it breaks structure.")

    return patterns


def find_support_resistance(
    bars: pd.DataFrame, *, pivot_window: int = 3, tolerance_pct: float = 0.15,
    max_levels: int = 12,
) -> list[PriceLevel]:
    """Cluster confirmed pivot highs/lows into horizontal levels.

    tolerance_pct is a percentage of price (0.15 means 0.15%), not a fraction.
    """
    if pivot_window < 1 or tolerance_pct <= 0 or max_levels < 1:
        raise ValueError("pivot_window, tolerance_pct and max_levels must be positive")
    df = _prepare(bars)
    candidates: list[tuple[float, str, int]] = []
    w = pivot_window
    for i in range(w, len(df) - w):
        high = float(df.iloc[i].high)
        low = float(df.iloc[i].low)
        if high > float(df.high.iloc[i-w:i].max()) and high >= float(df.high.iloc[i+1:i+w+1].max()):
            candidates.append((high, "resistance", i))
        if low < float(df.low.iloc[i-w:i].min()) and low <= float(df.low.iloc[i+1:i+w+1].min()):
            candidates.append((low, "support", i))

    # Greedy price clustering; each pivot is assigned once to the nearest
    # same-side cluster inside the relative tolerance band.
    clusters: list[dict] = []
    for price, kind, idx in sorted(candidates, key=lambda x: x[0]):
        matches = [
            (abs(price - c["price"]) / max(abs(c["price"]), 1e-12), j)
            for j, c in enumerate(clusters)
            if c["kind"] == kind
            and abs(price - c["price"]) / max(abs(c["price"]), 1e-12) * 100 <= tolerance_pct
        ]
        if matches:
            _, j = min(matches)
            c = clusters[j]
            c["prices"].append(price)
            c["indices"].append(idx)
            c["price"] = float(np.mean(c["prices"]))
        else:
            clusters.append({"kind": kind, "price": price, "prices": [price], "indices": [idx]})

    levels = [
        PriceLevel(round(c["price"], 10), c["kind"], len(c["indices"]),
                   min(c["indices"]), max(c["indices"]))
        for c in clusters
    ]
    # Prefer repeatedly tested levels, then most recently tested.
    levels.sort(key=lambda x: (x.touches, x.last_index), reverse=True)
    return levels[:max_levels]


def _trend_context(df: pd.DataFrame, period: int = 20) -> tuple[Direction, float]:
    close = df.close.astype(float)
    if len(close) < period + 2:
        return "neutral", 0.0
    ema = close.ewm(span=period, adjust=False).mean()
    slope = float((ema.iloc[-1] / ema.iloc[-period] - 1) * 100)
    if close.iloc[-1] > ema.iloc[-1] and slope > 0.05:
        return "bullish", min(1.0, abs(slope) / 2)
    if close.iloc[-1] < ema.iloc[-1] and slope < -0.05:
        return "bearish", min(1.0, abs(slope) / 2)
    return "neutral", min(1.0, abs(slope) / 2)


def analyse_price_action(
    bars: pd.DataFrame, *, lookback: int = 80, pivot_window: int = 3,
    tolerance_pct: float = 0.15, max_levels: int = 12,
) -> PriceActionResult:
    """Return candle patterns, horizontal levels and a simple EMA trend context."""
    df = _prepare(bars)
    patterns = detect_candle_patterns(df, lookback=lookback)
    levels = find_support_resistance(df, pivot_window=pivot_window,
                                     tolerance_pct=tolerance_pct, max_levels=max_levels)
    trend, strength = _trend_context(df)
    p = _candle_parts(df)
    last = len(df) - 1
    latest = {
        "open": float(df.iloc[last].open),
        "high": float(df.iloc[last].high),
        "low": float(df.iloc[last].low),
        "close": float(df.iloc[last].close),
        "body_ratio": float(p.iloc[last].body_ratio),
        "upper_wick_ratio": float(p.iloc[last].upper_wick / p.iloc[last]["range"]),
        "lower_wick_ratio": float(p.iloc[last].lower_wick / p.iloc[last]["range"]),
        "close_location": float(p.iloc[last].close_location),
    }
    return PriceActionResult(patterns, levels, trend, round(strength, 3), latest)


__all__ = [
    "CandlePattern", "PriceLevel", "PriceActionResult",
    "detect_candle_patterns", "find_support_resistance", "analyse_price_action",
]
