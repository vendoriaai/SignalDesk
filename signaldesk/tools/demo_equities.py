"""Demo equities fundamentals/earnings for offline deep dives and tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from .base import Source, ToolResult

_DEMO_FUN = {
    "AAPL": dict(longName="Apple Inc.", sector="Technology", industry="Consumer Electronics",
                 marketCap=3.4e12, trailingPE=28.5, forwardPE=24.1, priceToBook=52.0,
                 profitMargins=0.24, returnOnEquity=1.5, revenueGrowth=0.06, earningsGrowth=0.08,
                 recommendationKey="buy", targetMeanPrice=235.0),
    "MSFT": dict(longName="Microsoft Corp.", sector="Technology", industry="Software",
                 marketCap=3.1e12, trailingPE=32.0, forwardPE=27.0, priceToBook=11.0,
                 profitMargins=0.37, returnOnEquity=0.38, revenueGrowth=0.12, earningsGrowth=0.11,
                 recommendationKey="strong_buy", targetMeanPrice=480.0),
    "NVDA": dict(longName="NVIDIA Corp.", sector="Technology", industry="Semiconductors",
                 marketCap=3.0e12, trailingPE=45.0, forwardPE=28.0, priceToBook=42.0,
                 profitMargins=0.49, returnOnEquity=1.10, revenueGrowth=0.94, earningsGrowth=1.02,
                 recommendationKey="strong_buy", targetMeanPrice=155.0),
    "TSLA": dict(longName="Tesla Inc.", sector="Consumer Cyclical", industry="Auto Manufacturers",
                 marketCap=8.0e11, trailingPE=65.0, forwardPE=48.0, priceToBook=12.0,
                 profitMargins=0.07, returnOnEquity=0.10, revenueGrowth=0.02, earningsGrowth=-0.30,
                 recommendationKey="hold", targetMeanPrice=250.0),
}

_DEMO_ANNUALS = {
    "AAPL": [("2022", 394328e6, 99803e6), ("2023", 383285e6, 96995e6),
             ("2024", 391035e6, 93736e6), ("2025", 401000e6, 99000e6)],
    "MSFT": [("2022", 198270e6, 72738e6), ("2023", 211915e6, 72361e6),
             ("2024", 245122e6, 88136e6), ("2025", 265000e6, 96000e6)],
    "NVDA": [("2022", 26914e6, 9752e6), ("2023", 26974e6, 4368e6),
             ("2024", 60922e6, 29760e6), ("2025", 130497e6, 72880e6)],
    "TSLA": [("2022", 81462e6, 12556e6), ("2023", 96773e6, 14997e6),
             ("2024", 97690e6, 7153e6), ("2025", 95500e6, 8200e6)],
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class _DemoEqBase:
    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _source(name: str = "demo-generator") -> Source:
        return Source(name=name, url="offline://demo", retrieved_at=_now())

    @staticmethod
    def _generic(symbol: str) -> dict:
        return {**_DEMO_FUN["MSFT"], "longName": f"{symbol} Corp. (demo)"}


class DemoFundamentalsTool:
    name = "fundamentals"
    description = "Demo fundamentals."

    def __init__(self, artifacts_dir: Path):
        self._b = _DemoEqBase(artifacts_dir)

    def run(self, symbol: str) -> ToolResult:
        row = dict(_DEMO_FUN.get(symbol.upper(), _DemoEqBase._generic(symbol)))
        row["symbol"] = symbol.upper()
        frame = pd.DataFrame([row])
        path = self._b.artifacts_dir / f"fundamentals_{symbol.upper()}.csv"
        frame.to_csv(path, index=False)
        return ToolResult(csv_files=[path],
                          summary=f"{symbol}: {row['longName']} ({row['sector']})",
                          sources=[self._b._source()])


class DemoEdgarTool:
    name = "edgar_facts"
    description = "Demo EDGAR annuals."

    def __init__(self, artifacts_dir: Path):
        self._b = _DemoEqBase(artifacts_dir)

    def run(self, symbol: str) -> ToolResult:
        rows = _DEMO_ANNUALS.get(symbol.upper())
        if not rows:
            rows = [(str(y), 1e10 + y * 1e6, 1e9 + y * 1e5) for y in range(2022, 2026)]
        frame = pd.DataFrame(rows, columns=["fy", "revenue", "net_income"])
        path = self._b.artifacts_dir / f"edgar_{symbol.upper()}.csv"
        frame.to_csv(path, index=False)
        return ToolResult(csv_files=[path],
                          summary=f"{symbol}: EDGAR annuals FY{rows[0][0]}-FY{rows[-1][0]} ({len(rows)} rows)",
                          sources=[self._b._source()])


class DemoEarningsTool:
    name = "earnings"
    description = "Demo earnings calendar/history."

    def __init__(self, artifacts_dir: Path):
        self._b = _DemoEqBase(artifacts_dir)

    def run(self, symbol: str) -> ToolResult:
        today = datetime.now(UTC).date()
        rows = []
        for i in range(4, 0, -1):
            d = today - timedelta(days=91 * i)
            est = 1.40 + 0.05 * (4 - i)
            act = est + (0.04 if i % 2 == 0 else -0.02)
            rows.append({"earnings_date": d.isoformat(), "eps_estimate": round(est, 2),
                         "eps_actual": round(act, 2),
                         "surprise_pct": round((act / est - 1) * 100, 2)})
        next_date = today + timedelta(days=21)
        frame = pd.DataFrame(rows)
        path = self._b.artifacts_dir / f"earnings_{symbol.upper()}.csv"
        frame.to_csv(path, index=False)
        return ToolResult(csv_files=[path],
                          summary=f"{symbol}: next earnings {next_date.isoformat()}; {len(rows)} past quarters",
                          sources=[self._b._source()])
