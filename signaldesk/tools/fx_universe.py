"""Forex/metals universe construction (WF-3 Phase 2).

Crypto uses CoinGecko movers; FX spot has no free mover endpoint as canonical,
so we compute 24h changes for the candidate list ourselves from a single
batched yfinance download. Metals universe is the static pair list.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from ..markets import FOREX_CANDIDATES, METALS, vendor_symbol
from .base import Source, Tool, ToolResult


class YFinanceFXMoversTool(Tool):
    name = "fx_movers"
    description = "24h movers across the FX candidate list (yfinance)."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def run(self, per_side: int = 5) -> ToolResult:
        import yfinance as yf

        mapping = {s: vendor_symbol(s, "forex") for s in FOREX_CANDIDATES}
        try:
            raw = yf.download(
                list(mapping.values()), period="5d", interval="1d",
                group_by="ticker", progress=False, auto_adjust=False, threads=False,
            )
        except Exception as exc:
            return ToolResult(summary=f"FX movers unavailable: {exc}", degraded=True)

        rows = []
        for sym, ticker in mapping.items():
            try:
                frame = raw[ticker] if len(mapping) > 1 else raw
                closes = frame["Close"].dropna()
                if len(closes) < 2:
                    continue
                chg = float(closes.iloc[-1] / closes.iloc[-2] - 1) * 100
                rows.append({"symbol": sym, "price": float(closes.iloc[-1]),
                             "change_24h_pct": round(chg, 4), "volume": 0.0, "market_cap": None})
            except Exception:
                continue
        if not rows:
            return ToolResult(summary="FX movers unavailable (empty download)", degraded=True)
        frame = pd.DataFrame(rows)
        gainers = frame.nlargest(per_side, "change_24h_pct").assign(side="gainer")
        losers = frame.nsmallest(per_side, "change_24h_pct").assign(side="loser")
        out = pd.concat([gainers, losers])
        path = self.artifacts_dir / "movers.csv"
        out.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"{len(gainers)} FX gainers, {len(losers)} FX losers",
            sources=[Source(name="yfinance", retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"))],
        )


class StaticMetalsTool(Tool):
    name = "metals_universe"
    description = "Static metals universe (XAUUSD/XAGUSD)."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def run(self, per_side: int = 0) -> ToolResult:
        frame = pd.DataFrame(
            [{"symbol": s, "side": "universe", "change_24h_pct": None, "price": None,
              "volume": None, "market_cap": None} for s in METALS]
        )
        path = self.artifacts_dir / "movers.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path], summary=f"metals universe: {', '.join(METALS)}",
            sources=[Source(name="static", retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"))],
        )
