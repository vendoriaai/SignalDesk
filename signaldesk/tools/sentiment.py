"""Market sentiment indices: Fear & Greed + Altcoin Season (TAD FR-3)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from .base import Source, TokenBucket, Tool, ToolResult


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fng_regime(value: float) -> str:
    if value < 25:
        return "extreme fear"
    if value < 45:
        return "fear"
    if value < 55:
        return "neutral"
    if value < 75:
        return "greed"
    return "extreme greed"


class FearGreedTool(Tool):
    name = "fear_greed"
    description = "Crypto Fear & Greed Index (alternative.me)."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    def run(self) -> ToolResult:
        self._bucket.acquire()
        try:
            resp = httpx.get("https://api.alternative.me/fng/", params={"limit": 1}, timeout=15.0)
            resp.raise_for_status()
            entry = resp.json()["data"][0]
            value = int(entry["value"])
            classification = str(entry.get("value_classification") or fng_regime(value))
        except Exception as exc:
            return ToolResult(summary=f"fear & greed unavailable: {exc}", degraded=True)
        frame = pd.DataFrame([{"index": "fear_greed", "value": value, "regime": classification.lower()}])
        path = self.artifacts_dir / "fear_greed.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"Fear & Greed: {value} ({classification.lower()})",
            sources=[Source(name="alternative.me", url="https://api.alternative.me/fng/", retrieved_at=_now())],
        )


class AltSeasonTool(Tool):
    """Altcoin Season Index, computed CoinGecko-native.

    blockchaincenter's API endpoint is gone (404 since 2026-09), so the
    index is rebuilt from its original definition: the share of the top-50
    coins that outperformed BTC over the last 30 days. Same one-call
    CoinGecko endpoint the movers tool already uses.
    """

    name = "altcoin_season"
    description = "Altcoin Season Index: % of top-50 alts outperforming BTC over 30d."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    def run(self) -> ToolResult:
        self._bucket.acquire()
        try:
            resp = httpx.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 50,
                    "page": 1,
                    "price_change_percentage": "30d",
                },
                timeout=20.0,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:
            return ToolResult(summary=f"altcoin season index unavailable: {exc}", degraded=True)

        btc = next((r for r in rows if r.get("id") == "bitcoin"), None)
        btc_chg = btc.get("price_change_percentage_30d_in_currency") if btc else None
        if btc_chg is None:
            return ToolResult(summary="altcoin season index unavailable: no BTC 30d baseline",
                              degraded=True)
        alts = [float(r["price_change_percentage_30d_in_currency"]) for r in rows
                if r.get("id") != "bitcoin"
                and r.get("price_change_percentage_30d_in_currency") is not None]
        if not alts:
            return ToolResult(summary="altcoin season index unavailable: no alt 30d data",
                              degraded=True)
        value = round(sum(1 for chg in alts if chg > float(btc_chg)) / len(alts) * 100.0, 1)
        regime = "altcoin season" if value >= 75 else ("bitcoin season" if value <= 25 else "mixed")
        frame = pd.DataFrame([{"index": "altcoin_season", "value": value, "regime": regime}])
        path = self.artifacts_dir / "altcoin_season.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"Altcoin Season Index: {value} ({regime}) — {len(alts)} alts vs BTC 30d",
            sources=[Source(name="coingecko", url="https://api.coingecko.com/api/v3/coins/markets",
                            retrieved_at=_now())],
        )
