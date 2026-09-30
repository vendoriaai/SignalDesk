"""Deterministic offline data provider.

Generates a seeded synthetic crypto market so the full WF-1 pipeline runs with
zero network access: demo scans, tests, and the eval harness. All artifacts are
real CSVs in the run directory, exactly like the live tools produce.
"""
from __future__ import annotations

import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..markets import ENTRY_TF_WINDOWS
from .base import Source, Tool, ToolResult
from .search import SearchHit

_DAYS = 380  # enough history for 52w ranges and all rolling windows

# symbol -> base price, daily drift, daily sigma, cycle amplitude, cycle period, base volume
# Bases are CALIBRATED so the deterministic 380-day series ends at the real
# 2026-09-30 price (base x the seeded path multiplier = today's market price —
# BTC/ETH/BNB/SOL/XRP from live quotes, alts from yfinance where DNS allowed).
# The regime is a mild BEAR: BTC drifts below its 200d SMA so the
# dual-direction engine opens the SHORT book (the long book stands down,
# disclosed) — demo scans exercise SHORT signals the same way live bull scans
# exercise LONGs.
_DEMO_MARKET: dict[str, tuple[float, float, float, float, int, float]] = {
    "BTC": (77_896.968, -0.0002, 0.018, 0.004, 9, 2.1e9),
    "ETH": (2_026.477, -0.0009, 0.022, 0.005, 8, 1.2e9),
    "SOL": (118.671, -0.0008, 0.025, 0.005, 8, 4.5e8),
    "XRP": (0.96767, 0.0006, 0.020, 0.004, 9, 3.8e8),  # mild uptrend -> long, book-capped
    "BNB": (500.512, -0.0004, 0.015, 0.004, 10, 2.6e8),
    "SUI": (2.7562, -0.0011, 0.020, 0.003, 7, 1.9e8),
    "TAO": (54.726, 0.0007, 0.030, 0.006, 8, 9.0e7),
    "AAVE": (383.787, -0.0009, 0.025, 0.005, 8, 1.1e8),
    "ENA": (0.000134, 0.0200, 0.010, 0.001, 6, 2.2e8),  # parabolic demo runner (long-refused)
    "DOGE": (0.066781, -0.0010, 0.030, 0.006, 9, 1.5e8),
    "AVAX": (15.2967, -0.0007, 0.028, 0.005, 9, 1.2e8),
    "LINK": (13.5407, -0.0006, 0.026, 0.005, 8, 1.0e8),
}

# forex/metals demo series (no volume — spot FX)
_DEMO_FX: dict[str, tuple[float, float, float, float, int, float]] = {
    "EURUSD": (1.0850, 0.0006, 0.0040, 0.0015, 14, 0.0),
    "GBPUSD": (1.2720, 0.0004, 0.0045, 0.0015, 12, 0.0),
    "USDJPY": (149.50, -0.0004, 0.0050, 0.0020, 13, 0.0),
    "AUDUSD": (0.6630, 0.0007, 0.0048, 0.0018, 11, 0.0),
    "USDCAD": (1.3620, -0.0002, 0.0038, 0.0012, 12, 0.0),
    "NZDUSD": (0.6085, 0.0005, 0.0050, 0.0018, 11, 0.0),
    "USDCHF": (0.8810, -0.0003, 0.0042, 0.0015, 12, 0.0),
    "EURGBP": (0.8525, 0.0001, 0.0028, 0.0010, 13, 0.0),
    "EURJPY": (162.40, 0.0003, 0.0052, 0.0020, 12, 0.0),
    "GBPJPY": (190.30, 0.0002, 0.0058, 0.0022, 12, 0.0),
    "AUDJPY": (99.10, 0.0004, 0.0055, 0.0020, 11, 0.0),
    "EURCHF": (0.9560, 0.0001, 0.0030, 0.0010, 13, 0.0),
    "NZDJPY": (91.00, 0.0002, 0.0056, 0.0020, 11, 0.0),
    "XAUUSD": (2_655.0, 0.0011, 0.0080, 0.0030, 17, 1.6e8),
    "XAGUSD": (31.40, 0.0009, 0.0120, 0.0040, 15, 6.0e7),
}

# equities demo series (deep dive / future equity scans)
_DEMO_EQ: dict[str, tuple[float, float, float, float, int, float]] = {
    "AAPL": (228.0, 0.0010, 0.014, 0.006, 16, 5.2e7),
    "MSFT": (430.0, 0.0009, 0.013, 0.005, 15, 2.4e7),
    "NVDA": (138.0, 0.0028, 0.026, 0.008, 13, 3.1e8),
    "TSLA": (262.0, 0.0004, 0.028, 0.009, 12, 9.6e7),
    "AMZN": (196.0, 0.0011, 0.016, 0.006, 14, 4.4e7),
}

_TABLES = {"crypto": _DEMO_MARKET, "forex": _DEMO_FX, "metals": _DEMO_FX, "equities": _DEMO_EQ}


def _table_for(symbol: str) -> dict:
    s = symbol.upper()
    for table in (_DEMO_MARKET, _DEMO_FX, _DEMO_EQ):
        if s in table:
            return table
    raise KeyError(f"unknown demo symbol {symbol}")


_END = datetime.now(UTC).date() - timedelta(days=1)


def demo_intraday(symbol: str, interval: str) -> pd.DataFrame:
    """Synthetic intraday series consistent with the daily demo path.

    Deterministic per (symbol, interval); drift/sigma scaled from the daily
    profile so multi-timeframe scans line up roughly in direction.
    """
    table = _table_for(symbol)
    base, drift, sigma, amp, period, volume = table[symbol.upper()]
    minutes = {"1h": 60, "30m": 30, "15m": 15, "5m": 5, "1m": 1}[interval]
    bars_per_day = 1440 // minutes
    days = ENTRY_TF_WINDOWS.get(interval, ("5d", 14))[1]
    n = days * bars_per_day
    rng = np.random.default_rng(_seed(symbol + interval))
    sub_sigma = sigma / (bars_per_day ** 0.5)
    sub_drift = drift / bars_per_day
    t = np.arange(n)
    rets = sub_drift + rng.normal(0.0, sub_sigma, n) \
        + (amp / (bars_per_day ** 0.5)) * np.sin(2 * np.pi * t / (period * bars_per_day))
    close = base * np.exp(np.cumsum(rets))
    # rescale so the intraday series ends at the daily demo close — otherwise
    # entry refinement would compare LTF structure against a price far away
    close = close * (float(demo_ohlcv(symbol.upper())["close"].iloc[-1]) / close[-1])
    open_ = np.concatenate([[close[0] * (1 - rets[0])], close[:-1]])
    spread = np.abs(rng.normal(0, sub_sigma * 0.7, n)) + 0.0005
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    vol_arr = volume * np.clip(1 + rng.uniform(-0.3, 0.3, n), 0.2, None) / bars_per_day
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    dates = pd.date_range(end=end, periods=n, freq=interval.replace("m", "min").replace("h", "h"))
    return pd.DataFrame({"date": dates, "open": open_, "high": high, "low": low,
                         "close": close, "volume": vol_arr})


def _seed(symbol: str) -> int:
    return zlib.crc32(symbol.encode()) & 0x7FFFFFFF


def demo_ohlcv(symbol: str, days: int = _DAYS) -> pd.DataFrame:
    table = _table_for(symbol)
    base, drift, sigma, amp, period, volume = table[symbol.upper()]
    rng = np.random.default_rng(_seed(symbol))
    t = np.arange(days)
    rets = drift + rng.normal(0.0, sigma, days) + amp * np.sin(2 * np.pi * t / period)
    close = base * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0] * (1 - rets[0])], close[:-1]])
    spread = np.abs(rng.normal(0, sigma * 0.7, days)) + 0.002
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    streak = np.abs(rets[-1]) if days else 0.005
    close[-1] = close[-2] * (1 + rets[-1]) if days > 1 else close[-1]
    vol_noise = 1 + 0.4 * np.sin(2 * np.pi * t / 17) + rng.uniform(-0.2, 0.2, days) + streak
    volume_arr = volume * np.clip(vol_noise, 0.2, None)
    dates = pd.date_range(end=_END, periods=days, freq="D")
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume_arr}
    )


def demo_symbols(market: str = "crypto") -> list[str]:
    if market == "crypto":
        return list(_DEMO_MARKET)
    if market == "forex":
        return [s for s in _DEMO_FX if not s.startswith(("XAU", "XAG"))]
    if market == "metals":
        return [s for s in _DEMO_FX if s.startswith(("XAU", "XAG"))]
    if market == "equities":
        return list(_DEMO_EQ)
    return []


class _DemoBase(Tool):
    def __init__(self, artifacts_dir: Path, market: str = "crypto"):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.market = market

    def _table(self) -> dict:
        return _DEMO_MARKET if self.market == "crypto" else _DEMO_FX

    def _symbols(self) -> list[str]:
        return demo_symbols(self.market)

    @staticmethod
    def _source() -> Source:
        return Source(name="demo-generator", url="offline://demo", retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"))


class DemoMoversTool(_DemoBase):
    name = "market_movers"
    description = "Demo gainers/losers derived from the synthetic market."

    def run(self, per_side: int = 10) -> ToolResult:
        rows = []
        for sym in self._symbols():
            df = demo_ohlcv(sym)
            chg = (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
            label = f"{sym}USD" if self.market == "crypto" else sym
            rows.append({"symbol": label, "price": df["close"].iloc[-1], "change_24h_pct": chg,
                         "volume": df["volume"].iloc[-1], "market_cap": df["close"].iloc[-1] * 1e7})
        frame = pd.DataFrame(rows)
        gainers = frame.nlargest(per_side, "change_24h_pct").assign(side="gainer")
        losers = frame.nsmallest(per_side, "change_24h_pct").assign(side="loser")
        out = pd.concat([gainers, losers])
        path = self.artifacts_dir / "movers.csv"
        out.to_csv(path, index=False)
        return ToolResult(csv_files=[path], summary=f"{len(gainers)} gainers, {len(losers)} losers", sources=[self._source()])


class DemoQuotesTool(_DemoBase):
    name = "quotes"
    description = "Demo quotes from the synthetic market."

    def run(self, symbols: list[str]) -> ToolResult:
        rows = []
        for sym in symbols:
            key = sym[:-3] if (self.market == "crypto" and sym.upper().endswith("USD")) else sym
            df = demo_ohlcv(key)
            last, prev = df.iloc[-1], df.iloc[-2]
            tail = df.tail(min(360, len(df)))
            rows.append({
                "symbol": sym,
                "price": round(float(last["close"]), 8),
                "change_24h_pct": round(float((last["close"] / prev["close"] - 1) * 100), 4),
                "day_low": float(last["low"]), "day_high": float(last["high"]),
                "year_low": float(tail["low"].min()), "year_high": float(tail["high"].max()),
                "volume": float(last["volume"]),
            })
        out = pd.DataFrame(rows)
        path = self.artifacts_dir / "quotes.csv"
        out.to_csv(path, index=False)
        return ToolResult(csv_files=[path], summary=f"quotes for {len(rows)} symbols (demo)", sources=[self._source()])


class DemoOHLCVTool(_DemoBase):
    name = "ohlcv_history"
    description = "Demo OHLCV history from the synthetic market."

    def run(self, symbol: str, period: str = "6mo", interval: str = "1d") -> ToolResult:
        key = symbol[:-3] if (self.market == "crypto" and symbol.upper().endswith("USD")) else symbol
        if interval == "1d":
            frame = demo_ohlcv(key)
        else:
            frame = demo_intraday(key, interval)
        path = self.artifacts_dir / f"ohlcv_{symbol}_{interval}.csv"
        frame.to_csv(path, index=False)
        return ToolResult(
            csv_files=[path],
            summary=f"{symbol} {interval}: {len(frame)} bars",
            sources=[self._source()],
        )


class DemoMacroTool(_DemoBase):
    name = "macro_snapshot"
    description = "Demo DXY/10Y snapshot."

    def run(self) -> ToolResult:
        frame = pd.DataFrame([{"dxy": 104.2, "dxy_chg_30d_pct": -1.20,
                               "us10y": 4.05, "us10y_chg_30d_bp": -18.0,
                               "source": "demo-generator"}])
        path = self.artifacts_dir / "macro.csv"
        frame.to_csv(path, index=False)
        return ToolResult(csv_files=[path],
                          summary="DXY 104.20 (-1.20%/30d) · US10Y 4.05% (-18 bp/30d)",
                          sources=[self._source()])


class DemoFearGreedTool(_DemoBase):
    name = "fear_greed"
    description = "Demo Fear & Greed reading."

    def run(self) -> ToolResult:
        frame = pd.DataFrame([{"index": "fear_greed", "value": 38, "regime": "fear"}])
        path = self.artifacts_dir / "fear_greed.csv"
        frame.to_csv(path, index=False)
        return ToolResult(csv_files=[path], summary="Fear & Greed: 38 (fear)", sources=[self._source()])


class DemoAltSeasonTool(_DemoBase):
    name = "altcoin_season"
    description = "Demo Altcoin Season reading."

    def run(self) -> ToolResult:
        frame = pd.DataFrame([{"index": "altcoin_season", "value": 47, "regime": "mixed"}])
        path = self.artifacts_dir / "altcoin_season.csv"
        frame.to_csv(path, index=False)
        return ToolResult(csv_files=[path], summary="Altcoin Season Index: 47 (mixed)", sources=[self._source()])


class DemoSearchTool:
    """Canned dated claims so demo/test runs exercise the research phases."""

    name = "web_search"

    def __init__(self):
        from datetime import date

        self._today = date.today().isoformat()

    def available(self) -> bool:
        return True

    def query(self, q: str, max_results: int = 4) -> list[SearchHit]:
        return [
            SearchHit(
                title=f"Demo brief: {q}",
                url=f"https://demo.local/news/{abs(hash(q)) % 99999}",
                snippet=(
                    f"As of {self._today}, demo research note for '{q}': institutional flows "
                    "remain constructive, derivatives positioning is balanced, and analysts "
                    "flag upcoming protocol and macro catalysts."
                ),
                published=self._today,
            )
        ]

    def batch(self, queries: list[str], max_results: int = 3) -> dict[str, list[SearchHit]]:
        return {q: self.query(q, max_results) for q in queries}
