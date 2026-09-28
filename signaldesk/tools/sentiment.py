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
    name = "altcoin_season"
    description = "Altcoin Season Index (blockchaincenter)."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = TokenBucket(rate_per_sec=0.3, capacity=1)

    def run(self) -> ToolResult:
        self._bucket.acquire()
        try:
            resp = httpx.get(
                "https://www.blockchaincenter.net/api/altcoin-season-index.json",
                timeout=15.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            value = float(resp.json()["value"] if isinstance(resp.json(), dict) else resp.json())
        except Exception as exc:
            return ToolResult(summary=f"altcoin season index unavailable: {exc}", degraded=True)
        regime = "altcoin season" if value >= 75 else ("bitcoin season" if value <= 25 else "mixed")
        frame = pd.DataFrame([{"index": "altcoin_season", "value": value, "regime": regime}])
        path = self.artifacts_dir / "altcoin_season.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"Altcoin Season Index: {value} ({regime})",
            sources=[Source(name="blockchaincenter", url="https://www.blockchaincenter.net/altcoin-season-index/", retrieved_at=_now())],
        )
