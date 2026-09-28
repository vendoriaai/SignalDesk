"""US equities fundamentals (TAD FR-3 / roadmap Phase 2.13).

Primary: yfinance (info profile, ratios, earnings calendar/history).
Secondary: SEC EDGAR companyfacts for revenue/earnings history — official,
key-free, rate-limited by a declared User-Agent as SEC policy requires.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from .base import Source, TokenBucket, Tool, ToolResult


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FundamentalsTool(Tool):
    name = "fundamentals"
    description = "Profile, valuation ratios, margins for one ticker."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    def run(self, symbol: str) -> ToolResult:
        import yfinance as yf

        self._bucket.acquire()
        info = yf.Ticker(symbol).info or {}
        keys = [
            "longName", "sector", "industry", "marketCap", "trailingPE",
            "forwardPE", "priceToBook", "profitMargins", "returnOnEquity",
            "totalRevenue", "revenueGrowth", "earningsGrowth", "debtToEquity",
            "currentPrice", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
            "recommendationKey", "numberOfAnalystOpinions", "targetMeanPrice",
            "trailingEps", "forwardEps",
        ]
        row = {k: info.get(k) for k in keys if info.get(k) is not None}
        frame = pd.DataFrame([{"symbol": symbol, **row}])
        path = self.artifacts_dir / f"fundamentals_{symbol}.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"{symbol}: {row.get('longName', symbol)} ({row.get('sector', 'n/a')})",
            sources=[Source(name="yfinance", url=f"https://finance.yahoo.com/quote/{symbol}", retrieved_at=_now())],
        )


class EarningsTool(Tool):
    name = "earnings"
    description = "Earnings history + next date + analyst estimates."

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    def run(self, symbol: str) -> ToolResult:
        import yfinance as yf

        self._bucket.acquire()
        ticker = yf.Ticker(symbol)
        files: list[Path] = []
        parts: list[str] = []

        try:
            hist = ticker.get_earnings_dates(limit=8)
            if hist is not None and not hist.empty:
                hist = hist.reset_index()
                hist.columns = [str(c).lower().replace(" ", "_") for c in hist.columns]
                first = hist.columns[0]
                hist[first] = pd.to_datetime(hist[first]).dt.tz_localize(None)
                hist = hist.rename(columns={first: "earnings_date"})
                past = hist[hist["earnings_date"] <= pd.Timestamp.now()]
                fut = hist[hist["earnings_date"] > pd.Timestamp.now()]
                path = self.artifacts_dir / f"earnings_{symbol}.csv"
                past.to_csv(path, index=False)
                files.append(path)
                next_row = fut.iloc[0] if len(fut) else None
                if next_row is not None:
                    parts.append(f"next earnings {pd.Timestamp(next_row.iloc[0]).date()}")
                parts.append(f"{len(past)} past quarters")
        except Exception as exc:
            parts.append(f"earnings history unavailable ({exc})")

        return ToolResult(
            csv_files=files,
            summary=f"{symbol}: " + ("; ".join(parts) if parts else "no earnings data"),
            sources=[Source(name="yfinance", url=f"https://finance.yahoo.com/quote/{symbol}/analysis/", retrieved_at=_now())],
            degraded=not files,
        )


class EdgarFactsTool(Tool):
    """SEC EDGAR companyfacts: annual revenue & net income history."""

    name = "edgar_facts"
    description = "SEC EDGAR XBRL companyfacts (revenue, net income)."

    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    USER_AGENT = "SignalDesk/0.1 (contact: project maintainer)"

    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.USER_AGENT, "Accept-Encoding": "gzip, deflate"}

    def _cik(self, symbol: str) -> str | None:
        self._bucket.acquire()
        resp = httpx.get(self.TICKERS_URL, headers=self._headers(), timeout=20.0)
        resp.raise_for_status()
        for row in resp.json().values():
            if row["ticker"].upper() == symbol.upper():
                return f"CIK{int(row['cik_str']):010d}"
        return None

    def run(self, symbol: str) -> ToolResult:
        try:
            cik = self._cik(symbol)
            if cik is None:
                return ToolResult(summary=f"{symbol} not found in EDGAR tickers", degraded=True)
            self._bucket.acquire()
            resp = httpx.get(self.FACTS_URL.format(cik=cik), headers=self._headers(), timeout=20.0)
            resp.raise_for_status()
            facts = resp.json()
        except Exception as exc:
            return ToolResult(summary=f"EDGAR unavailable for {symbol}: {exc}", degraded=True)

        def annual(us_gaap_key: str) -> dict[str, float]:
            node = facts.get("facts", {}).get("us-gaap", {}).get(us_gaap_key, {})
            out: dict[str, float] = {}
            for unit, rows in node.get("units", {}).items():
                for r in rows:
                    if r.get("form") in ("10-K",) and r.get("fp") == "FY":
                        out[str(r.get("fy"))] = float(r.get("val", 0))
            return dict(sorted(out.items())[-5:])

        years = annual("RevenueFromContractWithCustomerExcludingAssessedTax") or annual("Revenues")
        income = annual("NetIncomeLoss")
        rows = [
            {"fy": fy, "revenue": years.get(fy), "net_income": income.get(fy)}
            for fy in sorted(set(years) | set(income))
        ]
        if not rows:
            return ToolResult(summary=f"no EDGAR facts for {symbol}", degraded=True)
        frame = pd.DataFrame(rows)
        path = self.artifacts_dir / f"edgar_{symbol}.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"{symbol}: EDGAR annuals FY{rows[0]['fy']}-FY{rows[-1]['fy']} ({len(rows)} rows)",
            sources=[Source(name="sec-edgar", url=self.FACTS_URL.format(cik=cik), retrieved_at=_now())],
        )
