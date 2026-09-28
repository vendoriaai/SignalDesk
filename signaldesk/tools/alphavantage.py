"""Alpha Vantage FX daily OHLCV (TAD FR-3). Requires a free API key; the
workflow falls back to yfinance when no key is configured (rule R4)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from .base import Source, TokenBucket, Tool, ToolResult

_API = "https://www.alphavantage.co/query"


class AlphaVantageFXTool(Tool):
    name = "fx_history_alphavantage"
    description = "FX daily OHLCV via Alpha Vantage (free key)."

    def __init__(self, artifacts_dir: Path, api_key: str | None):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self._bucket = TokenBucket(rate_per_sec=0.2, capacity=1)  # free tier: 25/day

    def available(self) -> bool:
        return bool(self.api_key)

    def run(self, symbol: str, days: int = 180) -> ToolResult:
        if not self.api_key:
            return ToolResult(summary="Alpha Vantage key not configured", degraded=True)
        base, quote = symbol[:3], symbol[3:]
        self._bucket.acquire()
        try:
            resp = httpx.get(
                _API,
                params={
                    "function": "FX_DAILY",
                    "from_symbol": base,
                    "to_symbol": quote,
                    "apikey": self.api_key,
                    "outputsize": "compact",
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            series = payload.get("Time Series FX (Daily)")
            if not series:
                raise ValueError(payload.get("Note") or payload.get("Information") or "no FX data")
        except Exception as exc:
            return ToolResult(summary=f"Alpha Vantage unavailable for {symbol}: {exc}", degraded=True)

        rows = [
            {
                "date": pd.Timestamp(day),
                "open": float(v["1. open"]),
                "high": float(v["2. high"]),
                "low": float(v["3. low"]),
                "close": float(v["4. close"]),
                "volume": 0.0,  # spot FX: no volume in this feed
            }
            for day, v in series.items()
        ]
        frame = pd.DataFrame(rows).sort_values("date").tail(days).reset_index(drop=True)
        path = self.artifacts_dir / f"ohlcv_{symbol}_1d.csv"  # AV FX_DAILY is daily only
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"{symbol}: {len(frame)} bars (Alpha Vantage)",
            sources=[Source(name="alphavantage", url=_API, retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"))],
        )
