"""Triple-barrier outcome resolver (P0 measurement infrastructure).

Walks daily bars forward from each ledger signal and reports what happened:
TP1, TP2, stop, or the time barrier (mark-to-market). The conventions below are
fixed in advance so a result cannot be re-labelled after the fact:

- Resolution starts on the **first bar after the decision bar**. The scan reads
  a closed daily bar; assuming a fill at that same close is the optimistic case
  the backtest literature warns about, and it is not the baseline.
- If one bar touches both the stop and a target, the **stop is assumed first**
  (pessimistic). Touching TP1 and TP2 within the same bar counts both as filled
  (both are inside the bar's range and neither is a loss).
- Declared exit policy: 50% off at TP1, the remainder runs to TP2 or the stop,
  and the stop is **not** moved to break-even (the evidence on breakeven stops
  is that they cost expectancy while flattering the win rate). The raw variants
  (`r_all_in`, `r_runner`) are stored so other policies can be re-scored later.
- Net R subtracts the record's cost-in-R once (a round trip).
- The time barrier marks to market rather than dropping the trade: censored
  signals are the marginal cases that decide whether an edge is real.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

RESOLVE_HORIZON_BARS = 14     # vertical barrier, in daily bars
OUTCOMES_FILENAME = "outcomes.jsonl"
TP1_R = 2.0                   # by construction: tp1 = entry + 2*risk
TP2_R = 3.0
TP1_FRACTION = 0.5            # declared scale-out


@dataclass
class Resolution:
    """What happened to one ledger signal."""
    signal_id: str
    symbol: str
    market: str = ""
    status: str = "open"          # tp2 | tp1 | sl | time | open | no_data
    bars: int = 0
    bars_to_tp1: int | None = None
    bars_to_tp2: int | None = None
    exit_price: float | None = None
    mfe_r: float = 0.0            # max favourable excursion, in R
    mae_r: float = 0.0            # max adverse excursion, in R
    r_all_in: float = 0.0         # 100% out at the first barrier touched
    r_runner: float = 0.0         # 100% runs to TP2 / stop / time
    r_gross: float = 0.0          # declared policy: 50% TP1 + 50% runner
    r_net: float = 0.0            # r_gross minus cost-in-R
    cost_in_r: float = 0.0
    risk_pct: float = 0.0
    bars_last: str = ""           # last bar date seen
    horizon_bars: int = 0
    resolved_at: str = ""
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _as_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except Exception:
        return None


def _round(x: float) -> float:
    return round(float(x), 4)


def resolve(record, bars: pd.DataFrame, *, horizon_bars: int = RESOLVE_HORIZON_BARS) -> Resolution:
    """Resolve one ledger record against `bars` (date/open/high/low/close, ascending)."""
    out = Resolution(
        signal_id=record.signal_id, symbol=record.symbol,
        market=getattr(record, "market", ""),
        cost_in_r=float(getattr(record, "cost_in_r", 0.0) or 0.0),
        risk_pct=float(getattr(record, "risk_pct", 0.0) or 0.0),
        horizon_bars=horizon_bars,
        resolved_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    entry = float(record.entry)
    stop = float(record.stop)
    risk = float(getattr(record, "risk", 0.0) or 0.0) or (entry - stop)
    if bars is None or bars.empty or risk <= 0:
        out.status = "no_data"
        return out

    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    anchor = _as_date(getattr(record, "bars_last_date", ""))
    if anchor is not None:
        frame = frame[frame["date"] > anchor]
    if frame.empty:
        out.status = "open"  # decision bar is the latest bar: nothing to resolve yet
        return out

    window = frame.iloc[:horizon_bars].reset_index(drop=True)
    tp1, tp2 = float(record.tp1), float(record.tp2)
    tp1_hit = False
    status = "open"
    exit_price: float | None = None
    bars_used = 0

    for i, bar in enumerate(window.itertuples(), start=1):
        high, low = float(bar.high), float(bar.low)
        bars_used = i
        if low <= stop:  # pessimistic: stop wins any same-bar tie
            status = "tp1" if tp1_hit else "sl"
            exit_price = stop
            break
        if high >= tp2:
            if not tp1_hit:
                tp1_hit, out.bars_to_tp1 = True, i
            out.bars_to_tp2 = i
            status, exit_price = "tp2", tp2
            break
        if high >= tp1 and not tp1_hit:
            tp1_hit, out.bars_to_tp1 = True, i

    if status == "open":  # nothing resolved inside the horizon -> time barrier
        status = "time"
        exit_price = float(window.iloc[-1]["close"])

    used = window.iloc[:bars_used]
    out.status = status
    out.bars = bars_used
    out.exit_price = _round(exit_price)
    out.mfe_r = _round((float(used["high"].max()) - entry) / risk)
    out.mae_r = _round((float(used["low"].min()) - entry) / risk)
    out.bars_last = str(used.iloc[-1]["date"])

    runner_r = (exit_price - entry) / risk
    out.r_runner = _round(runner_r)
    if status == "sl":
        out.r_all_in = _round(-1.0)
        out.r_gross = _round(runner_r)
    elif tp1_hit:
        out.r_all_in = _round(TP1_R)
        out.r_gross = _round(TP1_FRACTION * TP1_R + (1 - TP1_FRACTION) * runner_r)
    else:
        out.r_all_in = _round(runner_r)
        out.r_gross = _round(runner_r)
    out.r_net = _round(out.r_gross - out.cost_in_r)
    return out


def bars_provider_from_tool(ohlcv_tool, *, period: str = "2y") -> Callable[[str], pd.DataFrame]:
    """Daily bars for a symbol via an existing OHLCV tool (yfinance/demo)."""
    def _provider(symbol: str) -> pd.DataFrame:
        try:
            res = ohlcv_tool.run(symbol=symbol, period=period, interval="1d")
        except Exception:
            return pd.DataFrame()
        if not res.csv_files:
            return pd.DataFrame()
        return pd.read_csv(res.csv_files[0])

    return _provider


def resolve_records(records: Iterable, bars_provider: Callable[[str], pd.DataFrame], *,
                    horizon_bars: int = RESOLVE_HORIZON_BARS,
                    on_progress: Callable[[str], None] | None = None) -> list[Resolution]:
    """Resolve many records, fetching bars once per symbol."""
    cache: dict[str, pd.DataFrame] = {}
    out: list[Resolution] = []
    for rec in records:
        if rec.symbol not in cache:
            try:
                cache[rec.symbol] = bars_provider(rec.symbol)
            except Exception:
                cache[rec.symbol] = pd.DataFrame()
            if on_progress:
                on_progress(f"{rec.symbol}: {len(cache[rec.symbol])} bars")
        out.append(resolve(rec, cache[rec.symbol], horizon_bars=horizon_bars))
    return out


def outcomes_path(data_dir: Path) -> Path:
    return Path(data_dir) / OUTCOMES_FILENAME


def write_resolutions(path: Path, resolutions: list[Resolution]) -> int:
    path = Path(path)
    if not resolutions:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for res in resolutions:
            fh.write(res.to_json() + "\n")
    return len(resolutions)


def load_latest(path: Path) -> dict[str, dict]:
    """Latest resolution per signal_id (the file is append-only)."""
    path = Path(path)
    if not path.is_file():
        return {}
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        sid = row.get("signal_id")
        if not sid:
            continue
        prev = latest.get(sid)
        if prev is None or str(row.get("resolved_at", "")) >= str(prev.get("resolved_at", "")):
            latest[sid] = row
    return latest
