"""Market profiles: per-market symbol lists, vendor mapping, strategy preset.

A profile turns the generic WF-1 pipeline into WF-1 (crypto), WF-3 (forex /
metals) or equities slices without duplicating workflow code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FOREX_MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF"]
FOREX_CANDIDATES = FOREX_MAJORS + ["EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURCHF", "NZDJPY"]
METALS = ["XAUUSD", "XAGUSD"]
FX_VOLUME_AVAILABLE = False  # spot FX has no centralized tape; volume weight unused


@dataclass(frozen=True)
class MarketProfile:
    name: str
    majors: list[str]
    preset: str  # scoring preset key: "crypto" | "fx"
    crypto_symbols: tuple[str, ...] = ()
    metals_symbols: tuple[str, ...] = ()
    session_note: str = ""
    extra: dict = field(default_factory=dict)

# scan timeframes: primary (drives scoring) + lower timeframes (confluence)
DEFAULT_TIMEFRAMES = ("1d", "1h")

# entry-refinement timeframes (Phase 7.5): fetched only for the final top
# signals. yfinance caps 30m/15m/5m history at 60d and 1m at 7d per request,
# so each timeframe gets its own window: (live period, demo days).
DEFAULT_ENTRY_TIMEFRAMES = ("30m", "15m", "5m", "1m")
ENTRY_TF_WINDOWS: dict[str, tuple[str, int]] = {
    "1h": ("60d", 60),
    "30m": ("30d", 30),
    "15m": ("14d", 14),
    "5m": ("5d", 5),
    "1m": ("2d", 2),
}


def entry_tf_period(tf: str) -> str:
    """Live yfinance period for an intraday timeframe (safe fallback 5d)."""
    return ENTRY_TF_WINDOWS.get(tf, ("5d", 5))[0]


PROFILES: dict[str, MarketProfile] = {
    "crypto": MarketProfile(
        name="crypto",
        majors=["BTCUSD", "ETHUSD", "BNBUSD", "SOLUSD", "XRPUSD"],
        preset="crypto",
    ),
    "forex": MarketProfile(
        name="forex",
        majors=FOREX_MAJORS,
        preset="fx",
        session_note="Session note: London/New York overlap (12-16 UTC) drives follow-through; ATR quoted in pips.",
    ),
    "metals": MarketProfile(
        name="metals",
        majors=METALS,
        preset="fx",
        metals_symbols=tuple(METALS),
        session_note="Session note: metals follow COMEX/London fixes; risk-off macro dominates.",
    ),
    "equities": MarketProfile(
        name="equities",
        majors=[],
        preset="crypto",  # trend-momentum-v1 works as-is; no sentiment overlay
    ),
}


def vendor_symbol(symbol: str, market: str) -> str:
    """Internal symbol -> data-vendor ticker."""
    s = symbol.upper()
    if market == "crypto":
        return f"{s[:-3]}-USD" if s.endswith("USD") and len(s) > 3 else s
    if market == "metals":
        return {"XAUUSD": "GC=F", "XAGUSD": "SI=F"}.get(s, s)
    if market == "forex":
        return f"{s}=X" if len(s) == 6 and s.isalpha() else s
    return s  # equities pass through


def pip_size(symbol: str) -> float:
    """FX pip convention: JPY pairs 0.01, others 0.0001; non-FX returns NaN."""
    s = symbol.upper()
    if len(s) == 6 and s.isalpha():
        return 0.01 if "JPY" in s else 0.0001
    return float("nan")
