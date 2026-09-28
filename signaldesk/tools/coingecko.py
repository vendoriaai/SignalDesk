"""Crypto universe discovery via CoinGecko's free public API (no key)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from .base import Source, TokenBucket, Tool, ToolResult

_API = "https://api.coingecko.com/api/v3/coins/markets"


class CoinGeckoMoversTool(Tool):
    name = "market_movers"
    description = "Top gainers/losers by 24h change (crypto)."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    def run(self, per_side: int = 10) -> ToolResult:
        self._bucket.acquire()
        try:
            resp = httpx.get(
                _API,
                params={
                    "vs_currency": "usd",
                    "order": "volume_desc",
                    "per_page": 100,
                    "page": 1,
                    "price_change_percentage": "24h",
                },
                timeout=20.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # degrade per rule R4
            return ToolResult(summary=f"CoinGecko unavailable: {exc}", degraded=True)

        rows = [
            {
                "symbol": f"{c['symbol'].upper()}USD",
                "price": c.get("current_price"),
                "change_24h_pct": c.get("price_change_percentage_24h"),
                "volume": c.get("total_volume"),
                "market_cap": c.get("market_cap"),
            }
            for c in data
            if c.get("price_change_percentage_24h") is not None
        ]
        frame = pd.DataFrame(rows).dropna(subset=["change_24h_pct"])
        gainers = frame.nlargest(per_side, "change_24h_pct").assign(side="gainer")
        losers = frame.nsmallest(per_side, "change_24h_pct").assign(side="loser")
        out = pd.concat([gainers, losers])
        path = self.artifacts_dir / "movers.csv"
        out.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"{len(gainers)} gainers, {len(losers)} losers",
            sources=[Source(name="coingecko", url=_API, retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"))],
        )
