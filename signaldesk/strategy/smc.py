"""Deterministic, close-confirmed Smart Money Concepts primitives.

This module is deliberately independent of the existing scoring engine.
It consumes chronological OHLCV bars and emits explainable structures:
confirmed swing points, BOS/MSS, liquidity sweeps, fair-value gaps,
order blocks, mitigation state and a measured Fibonacci retracement.

No look-ahead: a pivot at index i is only available after ``right`` bars
have closed. A signal should only be evaluated on a completed candle.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import pandas as pd

Direction = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class Swing:
    index: int
    timestamp: str
    price: float
    kind: Literal["high", "low"]


@dataclass(frozen=True)
class StructureEvent:
    index: int
    timestamp: str
    direction: Direction
    event: Literal["BOS", "MSS"]
    broken_level: float
    swing_index: int


@dataclass(frozen=True)
class LiquiditySweep:
    index: int
    timestamp: str
    direction: Direction
    swept_level: float
    wick_extreme: float
    swing_index: int


@dataclass(frozen=True)
class FairValueGap:
    index: int
    timestamp: str
    direction: Direction
    lower: float
    upper: float
    midpoint: float
    mitigated: bool = False
    mitigation_index: int | None = None


@dataclass(frozen=True)
class OrderBlock:
    index: int
    timestamp: str
    direction: Direction
    lower: float
    upper: float
    midpoint: float
    displacement_index: int
    mitigated: bool = False
    mitigation_index: int | None = None


@dataclass
class SMCResult:
    swings: list[Swing]
    structure: list[StructureEvent]
    sweeps: list[LiquiditySweep]
    fvgs: list[FairValueGap]
    order_blocks: list[OrderBlock]
    fib: dict[str, float | str] | None

    def to_dict(self) -> dict:
        return {
            "swings": [asdict(x) for x in self.swings],
            "structure": [asdict(x) for x in self.structure],
            "liquidity_sweeps": [asdict(x) for x in self.sweeps],
            "fair_value_gaps": [asdict(x) for x in self.fvgs],
            "order_blocks": [asdict(x) for x in self.order_blocks],
            "fib_retracement": self.fib,
        }


def _validate_ohlcv(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"open", "high", "low", "close"}
    missing = required - {str(c).lower() for c in bars.columns}
    if missing:
        raise ValueError(f"OHLCV frame missing columns: {sorted(missing)}")
    frame = bars.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(frame) < 5:
        raise ValueError("at least five valid OHLC bars are required")
    if ((frame["high"] < frame["low"]) |
        (frame["high"] < frame[["open", "close"]].max(axis=1)) |
        (frame["low"] > frame[["open", "close"]].min(axis=1))).any():
        raise ValueError("invalid OHLC candle: high/low do not contain open and close")
    return frame


def _stamp(frame: pd.DataFrame, index: int) -> str:
    if "date" in frame.columns:
        return str(frame.iloc[index]["date"])
    if "datetime" in frame.columns:
        return str(frame.iloc[index]["datetime"])
    return str(index)


def find_swings(frame: pd.DataFrame, left: int = 3, right: int = 3) -> list[Swing]:
    """Return confirmed pivots; right-side bars are required for confirmation."""
    if left < 1 or right < 1:
        raise ValueError("left and right must be >= 1")
    df = _validate_ohlcv(frame)
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    out: list[Swing] = []
    for i in range(left, len(df) - right):
        hi = highs[i]
        lo = lows[i]
        # Strict comparison avoids marking flat equal-high/low plateaus as
        # multiple independent liquidity pools.
        if hi > max(highs[i-left:i]) and hi >= max(highs[i+1:i+right+1]):
            out.append(Swing(i, _stamp(df, i), float(hi), "high"))
        if lo < min(lows[i-left:i]) and lo <= min(lows[i+1:i+right+1]):
            out.append(Swing(i, _stamp(df, i), float(lo), "low"))
    return sorted(out, key=lambda x: (x.index, x.kind))


def _mitigation(df: pd.DataFrame, start: int, lower: float, upper: float,
                direction: Direction) -> int | None:
    for j in range(start + 1, len(df)):
        # A zone is considered mitigated on first subsequent wick overlap.
        if float(df.iloc[j]["low"]) <= upper and float(df.iloc[j]["high"]) >= lower:
            return j
    return None


def analyse_smc(
    bars: pd.DataFrame,
    *,
    left: int = 3,
    right: int = 3,
    displacement_atr: float = 1.2,
    atr_period: int = 14,
    max_order_block_lookback: int = 8,
) -> SMCResult:
    """Analyse one timeframe of completed OHLCV bars.

    BOS/MSS is close-confirmed beyond the most recent confirmed opposing
    swing. MSS means the break direction differs from the prior structure
    direction; BOS means it continues that direction.
    """
    df = _validate_ohlcv(bars)
    swings = find_swings(df, left=left, right=right)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    atr = (df["high"] - df["low"]).rolling(atr_period, min_periods=2).mean()
    structure: list[StructureEvent] = []
    sweeps: list[LiquiditySweep] = []
    fvgs: list[FairValueGap] = []
    blocks: list[OrderBlock] = []
    prior_structure: Direction | None = None
    used_highs: set[int] = set()
    used_lows: set[int] = set()

    # Only pivots confirmed before the current bar can be acted upon.
    for i in range(max(left + right + 1, 2), len(df)):
        row = df.iloc[i]
        prior_highs = [s for s in highs if s.index + right < i and s.index not in used_highs]
        prior_lows = [s for s in lows if s.index + right < i and s.index not in used_lows]
        close, high, low = float(row.close), float(row.high), float(row.low)

        if prior_highs:
            level = prior_highs[-1]
            if high > level.price and close < level.price:
                sweeps.append(LiquiditySweep(i, _stamp(df, i), "bearish",
                                             level.price, high, level.index))
                used_highs.add(level.index)
            elif close > level.price:
                direction: Direction = "bullish"
                kind = "BOS" if prior_structure in (None, direction) else "MSS"
                structure.append(StructureEvent(i, _stamp(df, i), direction,
                                                kind, level.price, level.index))
                prior_structure = direction
                used_highs.add(level.index)
                _make_order_block(df, i, direction, atr, displacement_atr,
                                  max_order_block_lookback, blocks)

        if prior_lows:
            level = prior_lows[-1]
            if low < level.price and close > level.price:
                sweeps.append(LiquiditySweep(i, _stamp(df, i), "bullish",
                                             level.price, low, level.index))
                used_lows.add(level.index)
            elif close < level.price:
                direction = "bearish"
                kind = "BOS" if prior_structure in (None, direction) else "MSS"
                structure.append(StructureEvent(i, _stamp(df, i), direction,
                                                kind, level.price, level.index))
                prior_structure = direction
                used_lows.add(level.index)
                _make_order_block(df, i, direction, atr, displacement_atr,
                                  max_order_block_lookback, blocks)

    # Detect FVGs independently of swing confirmation.
    # A three-candle gap can form before enough bars exist
    # to confirm a swing pivot.
    for i in range(2, len(df)):
        old_high = float(df.iloc[i - 2].high)
        old_low = float(df.iloc[i - 2].low)
        high = float(df.iloc[i].high)
        low = float(df.iloc[i].low)

        if low > old_high:
            end = _mitigation(df, i, old_high, low, "bullish")
            fvgs.append(
                FairValueGap(
                    i, _stamp(df, i), "bullish",
                    old_high, low, (old_high + low) / 2,
                    end is not None, end
                )
            )
        elif high < old_low:
            end = _mitigation(df, i, high, old_low, "bearish")
            fvgs.append(
                FairValueGap(
                    i, _stamp(df, i), "bearish",
                    high, old_low, (high + old_low) / 2,
                    end is not None, end
                )
            )

    fib = _fib_retracement(df, swings)
    return SMCResult(swings, structure, sweeps, fvgs, blocks, fib)


def _make_order_block(df: pd.DataFrame, break_index: int, direction: Direction,
                      atr: pd.Series, displacement_atr: float, lookback: int,
                      out: list[OrderBlock]) -> None:
    start = max(0, break_index - lookback)
    displacement = float(df.iloc[break_index].close - df.iloc[break_index].open)
    scale = float(atr.iloc[break_index])
    if not np.isfinite(scale) or scale <= 0 or abs(displacement) < scale * displacement_atr:
        return
    for j in range(break_index - 1, start - 1, -1):
        o, c = float(df.iloc[j].open), float(df.iloc[j].close)
        opposite = (direction == "bullish" and c < o) or (direction == "bearish" and c > o)
        if opposite:
            lo, hi = float(df.iloc[j].low), float(df.iloc[j].high)
            mitigated_at = _mitigation(df, break_index, lo, hi, direction)
            out.append(OrderBlock(j, _stamp(df, j), direction, lo, hi, (lo + hi) / 2,
                                  break_index, mitigated_at is not None, mitigated_at))
            return


def _fib_retracement(df: pd.DataFrame, swings: list[Swing]) -> dict[str, float | str] | None:
    """Fib levels for the latest confirmed impulse defined by adjacent opposite pivots."""
    if len(swings) < 2:
        return None
    a, b = swings[-2], swings[-1]
    if a.kind == b.kind or a.index >= b.index:
        return None
    high = a.price if a.kind == "high" else b.price
    low = a.price if a.kind == "low" else b.price
    span = high - low
    if span <= 0:
        return None
    bullish = a.kind == "low" and b.kind == "high"
    levels = (0.236, 0.382, 0.5, 0.618, 0.705, 0.786)
    result: dict[str, float | str] = {"direction": "bullish" if bullish else "bearish"}
    for ratio in levels:
        price = high - span * ratio if bullish else low + span * ratio
        result[f"{ratio:.3f}"] = round(float(price), 10)
    return result


def timeframe_confluence(results: dict[str, SMCResult]) -> dict:
    """Summarise latest structure direction across supplied timeframe results."""
    latest: dict[str, str] = {}
    for timeframe, result in results.items():
        if result.structure:
            latest[timeframe] = result.structure[-1].direction
    bullish = sum(v == "bullish" for v in latest.values())
    bearish = sum(v == "bearish" for v in latest.values())
    return {
        "latest_structure": latest,
        "bullish_timeframes": bullish,
        "bearish_timeframes": bearish,
        "alignment": ("bullish" if bullish and not bearish else
                      "bearish" if bearish and not bullish else
                      "mixed" if bullish or bearish else "unconfirmed"),
    }


__all__ = ["Swing", "StructureEvent", "LiquiditySweep", "FairValueGap",
           "OrderBlock", "SMCResult", "find_swings", "analyse_smc",
           "timeframe_confluence"]
