"""Macro snapshot: DXY (dollar index) and US 10Y yield (roadmap Phase 2.14).

Prefers FRED (DTWEXBGS / DGS10) when a key is configured; falls back to
yfinance tickers DX-Y.NYB and ^TNX; degrades with disclosure per R4.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from .base import Source, TokenBucket, Tool, ToolResult


@dataclass
class MacroSnapshot:
    dxy: float | None
    dxy_chg_30d_pct: float | None
    us10y: float | None
    us10y_chg_30d_bp: float | None
    source: str


class MacroSnapshotTool(Tool):
    name = "macro_snapshot"
    description = "DXY and US 10Y yield with 30-day changes."

    def __init__(self, artifacts_dir: Path, fred_api_key: str | None = None):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.fred_api_key = fred_api_key
        self._bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    def run(self) -> ToolResult:
        data: MacroSnapshot | None = None
        errors: list[str] = []
        if self.fred_api_key:
            data, err = self._from_fred(), None
            if data is None:
                errors.append("FRED fetch failed")
        if data is None:
            data = self._from_yfinance()
            if data is None:
                errors.append("yfinance macro fetch failed")
        if data is None:
            return ToolResult(summary=f"macro snapshot unavailable ({'; '.join(errors)})", degraded=True)

        frame = pd.DataFrame(
            [{
                "dxy": data.dxy,
                "dxy_chg_30d_pct": data.dxy_chg_30d_pct,
                "us10y": data.us10y,
                "us10y_chg_30d_bp": data.us10y_chg_30d_bp,
                "source": data.source,
            }]
        )
        path = self.artifacts_dir / "macro.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=(
                f"DXY {data.dxy} ({data.dxy_chg_30d_pct:+.2f}%/30d) · "
                f"US10Y {data.us10y:.2f}% ({data.us10y_chg_30d_bp:+.0f} bp/30d)"
                if data.dxy is not None and data.us10y is not None
                else "partial macro snapshot"
            ),
            sources=[Source(name=data.source, retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"))],
        )

    def _fred_series(self, series_id: str) -> pd.Series:
        resp = httpx.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": self.fred_api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 45,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        obs = [o for o in resp.json().get("observations", []) if o.get("value") not in (".", None)]
        vals = pd.Series(
            [float(o["value"]) for o in obs],
            index=pd.to_datetime([o["date"] for o in obs]),
        )
        return vals.sort_index()

    def _from_fred(self) -> MacroSnapshot | None:
        self._bucket.acquire()
        try:
            dxy = self._fred_series("DTWEXBGS")
            us10y = self._fred_series("DGS10")
        except Exception:
            return None
        try:
            n = min(21, len(dxy) - 1)
            return MacroSnapshot(
                dxy=float(dxy.iloc[-1]),
                dxy_chg_30d_pct=float(dxy.iloc[-1] / dxy.iloc[-1 - n] - 1) * 100,
                us10y=float(us10y.iloc[-1]),
                us10y_chg_30d_bp=float(us10y.iloc[-1] - us10y.iloc[-1 - min(21, len(us10y) - 1)]) * 100,
                source="FRED",
            )
        except Exception:
            return None

    def _yf_series(self, ticker: str) -> pd.Series | None:
        import yfinance as yf

        raw = yf.download(ticker, period="4mo", interval="1d", progress=False, auto_adjust=False)
        if raw.empty:
            return None
        closes = raw["Close"]
        if isinstance(closes, pd.DataFrame):
            closes = closes.iloc[:, 0]
        return closes.dropna()

    def _from_yfinance(self) -> MacroSnapshot | None:
        try:
            dxy = self._yf_series("DX-Y.NYB")
            tnx = self._yf_series("^TNX")
        except Exception:
            return None
        if dxy is None or tnx is None or len(dxy) < 25 or len(tnx) < 25:
            return None
        return MacroSnapshot(
            dxy=float(dxy.iloc[-1]),
            dxy_chg_30d_pct=float(dxy.iloc[-1] / dxy.iloc[-22] - 1) * 100,
            us10y=float(tnx.iloc[-1]),
            us10y_chg_30d_bp=float(tnx.iloc[-1] - tnx.iloc[-22]) * 100,
            source="yfinance (DX-Y.NYB, ^TNX)",
        )
