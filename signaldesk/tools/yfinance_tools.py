"""yfinance-backed market data: quotes snapshot + OHLCV history (TAD FR-3).

Market-aware via markets.vendor_symbol: crypto X-USD, forex X= Y=X, metals
GC=F/SI=F, equities pass-through.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from ..markets import vendor_symbol
from .base import Source, Tool, ToolResult


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalize_yf(raw: pd.DataFrame) -> pd.DataFrame:
    """Reduce a yfinance download frame to date/open/high/low/close/volume.

    Returns an *empty* frame (never raises) when Yahoo handed back nothing
    usable — an empty response or an unnamed reset index used to surface as a
    cryptic `KeyError: 'date'` that dropped the symbol from the scan.
    """
    empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    if raw is None or raw.empty:
        return empty
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str.lower).reset_index()
    date_col = next((c for c in ("date", "Date", "Datetime", "datetime", "index") if c in df.columns), None)
    if date_col is None:
        return empty
    df = df.rename(columns={date_col: "date"})
    keep = ["date", "open", "high", "low", "close", "volume"]
    if not set(keep) <= set(df.columns):
        return empty
    out = df[keep].dropna(subset=["close"]).reset_index(drop=True)
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    # normalize to naive UTC so daily/intraday CSVs are comparable and stable
    out["date"] = pd.to_datetime(out["date"], utc=True).dt.tz_localize(None)
    return out


class YFinanceQuotesTool(Tool):
    """One batched 1y daily download per universe; quotes are derived from
    the last bar, year high/low from the full window."""

    name = "quotes"
    description = "Snapshot: price, 24h change, day/year range, volume."

    def __init__(self, artifacts_dir: Path, market: str = "crypto"):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.market = market

    def run(self, symbols: list[str]) -> ToolResult:
        import time

        import yfinance as yf

        tickers = {s: vendor_symbol(s, self.market) for s in symbols}
        rows: list[dict] = []
        failed = list(tickers)
        # An empty batch is Yahoo throttling or a local DNS hiccup (yfinance
        # swallows curl errors into empty frames), not an answer — back off.
        for delay in (0.0, 1.5, 4.0):
            if delay:
                time.sleep(delay)
            rows, failed = self._fetch(yf, tickers)
            if rows:
                break
        out = pd.DataFrame(rows)
        path = self.artifacts_dir / "quotes.csv"
        out.to_csv(path, index=False)
        summary = f"quotes for {len(rows)}/{len(symbols)} symbols"
        if failed:
            summary += f"; failed: {', '.join(failed)}"
        return ToolResult(
            csv_files=[path], summary=summary, degraded=bool(failed),
            sources=[Source(name="yfinance", url="https://query1.finance.yahoo.com", retrieved_at=_now())],
        )

    def _fetch(self, yf, tickers: dict[str, str]) -> tuple[list[dict], list[str]]:
        if len(tickers) == 1:
            raw = yf.download(next(iter(tickers.values())), period="1y", interval="1d",
                              progress=False, auto_adjust=False, threads=False, timeout=20)
        else:
            raw = yf.download(
                list(tickers.values()), period="1y", interval="1d",
                group_by="ticker", progress=False, auto_adjust=False, threads=False,
                timeout=20,
            )
        rows: list[dict] = []
        failed: list[str] = []
        for sym, ticker in tickers.items():
            try:
                frame = raw[ticker] if len(tickers) > 1 else raw
                frame = _normalize_yf(frame)
                if frame.empty:
                    raise ValueError("empty frame")
                last, prev = frame.iloc[-1], frame.iloc[-2]
                change = (last["close"] / prev["close"] - 1) * 100 if prev["close"] else 0.0
                rows.append(
                    {
                        "symbol": sym,
                        "price": round(float(last["close"]), 8),
                        "change_24h_pct": round(float(change), 4),
                        "day_low": float(last["low"]),
                        "day_high": float(last["high"]),
                        "year_low": float(frame["low"].min()),
                        "year_high": float(frame["high"].max()),
                        "volume": float(last["volume"]),
                    }
                )
            except Exception:
                failed.append(sym)
        return rows, failed


class YFinanceOHLCVTool(Tool):
    name = "ohlcv_history"
    description = "Daily OHLCV history per symbol."

    def __init__(self, artifacts_dir: Path, market: str = "crypto"):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.market = market

    def run(self, symbol: str, period: str = "6mo", interval: str = "1d") -> ToolResult:
        import time

        import yfinance as yf

        ticker = vendor_symbol(symbol, self.market)
        frame = _normalize_yf(None)
        # Throttling/DNS blips surface as empty frames; back off twice before
        # declaring the symbol missing (genuinely unknown tickers fail fast).
        for delay in (0.0, 1.5, 4.0):
            if delay:
                time.sleep(delay)
            raw = yf.download(ticker, period=period, interval=interval, progress=False,
                              auto_adjust=False, timeout=20)
            frame = _normalize_yf(raw)
            if not frame.empty:
                break
        if frame.empty:
            raise RuntimeError(f"no OHLCV data for {symbol} ({ticker}, {interval}, {period})")
        path = self.artifacts_dir / f"ohlcv_{symbol}_{interval}.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"{symbol} {interval}: {len(frame)} bars {frame['date'].iloc[0].date()}..{frame['date'].iloc[-1].date()}",
            sources=[Source(name="yfinance", url=f"https://finance.yahoo.com/quote/{ticker}", retrieved_at=_now())],
        )
