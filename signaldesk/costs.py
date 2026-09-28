"""Trading-cost model: round-trip assumptions per market/symbol (P0).

Why this exists: a signal is only worth taking if its risk unit R is large
relative to the cost of round-tripping the position. Cost-in-R is
`cost_pct / risk_pct`; at 0.5R of cost, a 2R target needs a 66% win rate just to
break even. So the scanner (a) cites the assumed cost, (b) floors every stop so
cost stays a small fraction of R (strategy.scoring.MIN_RISK_COST_MULT), and
(c) stores the assumption in the ledger so outcomes can be re-scored at real
costs later.

Assumptions are conservative retail defaults — taker on both sides, one spread,
modest slippage — and are meant to be replaced with venue-specific numbers.
They are provenance-tracked as citations (`source_tool="cost_model"`), never
presented as market data.
"""
from __future__ import annotations

# round trip (buy + sell) as % of notional, by market
DEFAULT_ROUND_TRIP_COST_PCT: dict[str, float] = {
    "crypto": 0.40,    # 0.10%/side taker + alt-coin spread + slippage
    "forex": 0.03,     # ~1 pip spread on majors + commission
    "metals": 0.03,    # ~30c round-trip spread on gold + slippage
    "equities": 0.05,  # commission-free + SEC/TAF fees + 1c spread + slippage
}

# majors are cheaper than the long tail (deeper books, tighter spreads)
SYMBOL_ROUND_TRIP_COST_PCT: dict[str, float] = {
    "BTCUSD": 0.25,
    "ETHUSD": 0.25,
    "EURUSD": 0.02,
    "USDJPY": 0.02,
    "XAUUSD": 0.03,
    "XAGUSD": 0.05,
}

# Perpetual-futures funding paid by the long side (crypto carry is a long tax).
# ~0.01%/8h baseline = 10.95%/yr; realized BTC/ETH averages run 3-4%/yr.
CRYPTO_FUNDING_ANNUAL_PCT = 4.0

FALLBACK_COST_PCT = 0.10


def round_trip_cost_pct(market: str, symbol: str) -> float:
    """Assumed round-trip cost for one symbol, in percent of notional."""
    return SYMBOL_ROUND_TRIP_COST_PCT.get(
        symbol.upper(), DEFAULT_ROUND_TRIP_COST_PCT.get(market.lower(), FALLBACK_COST_PCT)
    )


def carry_cost_pct(market: str, hold_days: float) -> float:
    """Funding/swap drag for holding `hold_days` (crypto perps; 0 elsewhere)."""
    if market.lower() != "crypto" or hold_days <= 0:
        return 0.0
    return CRYPTO_FUNDING_ANNUAL_PCT / 365.0 * hold_days


def cost_in_r(cost_pct: float, risk_pct: float) -> float:
    """Cost expressed in R units (the number the report must show)."""
    if risk_pct <= 0:
        return float("inf")
    return cost_pct / risk_pct


def breakeven_win_rate(cost_r: float, target_r: float) -> float:
    """p = (1 + c_R) / (m + 1): driftless break-even hit rate for an m-R target.

    At zero cost this is the textbook 1/(m+1) — 33.3% for a 2R target — and it
    rises with the cost-in-R of the trade.
    """
    return (1.0 + cost_r) / (target_r + 1.0)
