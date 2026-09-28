"""Live mark-to-market for open signals (WF-5).

The triple-barrier resolver speaks in closed daily bars: a signal still
inside its 14-bar horizon is "open" and says nothing about P&L. This module
answers the question the user has in the meantime — is the signal profitable
*right now*? — by fetching a current quote per open signal and computing the
unrealized R with the record's own risk unit (direction-aware).

It is deliberately a snapshot, not a resolution: barriers are still confirmed
only on closed daily bars (rule R7 conventions unchanged) and the number is
labelled mark-to-market everywhere it is shown. The snapshot persists to
`mtm.json` so the dashboard reads a cache — the scheduler refreshes it on
every hourly check, and request handlers never fetch quotes. If every quote
fetch degrades, the previous snapshot is kept (rule R4).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

MTM_FILENAME = "mtm.json"
OPEN_STATUSES = ("open", "no_data")
MIN_REFRESH_INTERVAL_S = 120   # request handlers may not hammer the data source

_last_refresh = 0.0            # time.monotonic of the last actual fetch attempt
_refresh_lock = threading.Lock()


def mtm_path(data_dir: Path) -> Path:
    return Path(data_dir) / MTM_FILENAME


def load_mtm(data_dir: Path) -> dict:
    path = mtm_path(data_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_mtm(data_dir: Path, payload: dict) -> Path:
    path = mtm_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def open_signals(data_dir: Path, *, include_demo: bool = False) -> list[dict]:
    """Ledger rows whose latest resolution is still open (or unresolved)."""
    from . import resolver as resolver_mod

    rows = resolver_mod.signal_rows(data_dir, include_demo=include_demo, limit=100000)
    return [r for r in rows if r.get("status", "open") in OPEN_STATUSES]


def _quotes_tool(data_dir: Path, market: str):
    from .tools.yfinance_tools import YFinanceQuotesTool

    return YFinanceQuotesTool(Path(data_dir) / "mtm_quotes", market=market)


def unrealized(record: dict, price: float) -> tuple[float | None, float | None]:
    """Direction-aware (unrealized R, P&L %) for one ledger row at `price`.

    % P&L answers "how much am I up/down on this entry"; R keeps the number
    comparable to the outcome statistics (both flip sign for SHORTs).
    """
    entry = float(record.get("entry") or 0.0)
    # the ledger stores risk as entry - stop, which is negative for SHORTs
    risk = abs(float(record.get("risk") or 0.0) or (entry - float(record.get("stop") or 0.0)))
    if entry <= 0 or risk <= 0:
        return None, None
    sign = 1.0 if str(record.get("direction") or "LONG").upper() == "LONG" else -1.0
    move = sign * (price - entry)
    return round(move / risk, 4), round(move / entry * 100.0, 3)


def refresh(data_dir: Path, *, include_demo: bool = False,
            tool_builder=None, now: datetime | None = None) -> dict | None:
    """Quote every open signal and persist the snapshot.

    Returns the new payload, or None when nothing changed (no open signals
    would clear the file; a fully degraded fetch keeps the old one).
    """
    global _last_refresh
    with _refresh_lock:
        _last_refresh = time.monotonic()
    rows = open_signals(data_dir, include_demo=include_demo)
    if not rows:
        payload = {"fetched_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
                   "rows": {}}
        save_mtm(data_dir, payload)
        return payload
    out: dict[str, dict] = {}
    errors: list[str] = []
    for market in sorted({r["market"] for r in rows}):
        group = [r for r in rows if r["market"] == market]
        prices: dict[str, float] = {}
        try:
            tool = tool_builder(market) if tool_builder else _quotes_tool(data_dir, market)
            res = tool.run(symbols=[r["symbol"] for r in group])
            frame = pd.DataFrame()
            if res.csv_files:
                try:
                    frame = pd.read_csv(res.csv_files[0])
                except Exception:
                    frame = pd.DataFrame()  # empty CSV from a throttled batch
            if not frame.empty:
                for row in frame.itertuples():
                    p = getattr(row, "price", None)
                    if p is not None and pd.notna(p):
                        prices[str(row.symbol)] = float(p)
        except Exception as exc:
            errors.append(f"{market}: {type(exc).__name__}: {exc}")
            continue
        if not prices:
            errors.append(f"{market}: no quotes returned")
            continue
        for r in group:
            price = prices.get(r["symbol"])
            if price is None:
                continue
            r_unreal, pnl_pct = unrealized(r, price)
            if r_unreal is None:
                continue
            out[r["signal_id"]] = {"symbol": r["symbol"], "market": market,
                                   "price": price, "r_unrealized": r_unreal,
                                   "pnl_pct": pnl_pct}

    if not out:
        # nothing quotable this pass: keep the previous snapshot (R4: degrade,
        # don't erase) — only "no open signals at all" legitimately clears rows
        return None
    payload = {"fetched_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
               "rows": out}
    save_mtm(data_dir, payload)
    return payload


def refresh_if_stale(data_dir: Path, *, include_demo: bool = False,
                     tool_builder=None,
                     min_interval_s: int = MIN_REFRESH_INTERVAL_S) -> dict:
    """Throttled refresh for request handlers.

    Runs a full refresh only when the last fetch attempt is older than
    `min_interval_s`; otherwise serves the cached snapshot. Either way the
    current `mtm.json` content is returned, so callers can just send it.
    """
    global _last_refresh
    with _refresh_lock:
        due = time.monotonic() - _last_refresh >= min_interval_s
    if due:
        refresh(data_dir, include_demo=include_demo, tool_builder=tool_builder)
    return load_mtm(data_dir)
