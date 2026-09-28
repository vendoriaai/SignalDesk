"""Append-only signal ledger (P0 measurement infrastructure).

Every emitted signal is recorded with the numbers needed to score it later:
levels, risk unit, the assumed round-trip cost, a hash of the OHLCV snapshot it
was derived from, and a fingerprint of the scoring weights. `signaldesk
outcomes` reads this file, resolves each signal against subsequent bars, and
reports expectancy in R — the only honest way to tell whether a rule change
helped.

Design rules:
- Append-only JSONL; a signal_id is written once. Late re-reads dedupe by id.
- One file at `<data_dir>/signals.jsonl` (see `default_ledger_path`).
- Nothing here is a promise about returns: the ledger exists to *measure*.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from signaldesk import costs as cost_model
from signaldesk.strategy import scoring

LEDGER_FILENAME = "signals.jsonl"


class LedgerRecord(BaseModel):
    """One emitted signal, frozen at decision time."""
    signal_id: str                    # f"{run_id}:{symbol}"
    created_at: str
    as_of: str = ""                   # decision timestamp (report as_of)
    run_id: str
    market: str
    symbol: str
    direction: str = "LONG"
    score: float
    preset: str = ""
    weights_hash: str = ""
    entry: float
    stop: float
    tp1: float
    tp2: float
    entry_mode: str = "daily"         # daily | market | pullback
    risk: float = 0.0
    risk_pct: float = 0.0
    cost_pct: float = 0.0             # assumed round-trip cost, % of notional
    cost_in_r: float = 0.0
    data_hash: str = ""               # sha256 of the OHLCV CSV used for scoring
    bars_last_date: str = ""          # decision bar date (resolution anchor)
    source: str = "scan"              # scan | backfill
    demo: bool = False                # synthetic data — excluded from stats
    app_version: str = ""
    snapshots: list[str] = Field(default_factory=list)


def sha256_file(path: Path | str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / LEDGER_FILENAME


def default_ledger_path(run_dir: Path) -> Path:
    """`<home>/runs/<id>` -> `<home>/signals.jsonl`; anything else stays local."""
    run_dir = Path(run_dir)
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent / LEDGER_FILENAME
    return run_dir / LEDGER_FILENAME


def read_records(path: Path) -> list[LedgerRecord]:
    """Read the ledger, skipping malformed lines (append-only files get truncated lines)."""
    path = Path(path)
    if not path.is_file():
        return []
    out: list[LedgerRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(LedgerRecord(**json.loads(line)))
        except Exception:
            continue
    return out


def read_ids(path: Path) -> set[str]:
    return {r.signal_id for r in read_records(path)}


def append_records(path: Path, records: list[LedgerRecord]) -> int:
    """Append new records; ids already present are skipped. Returns count written."""
    path = Path(path)
    if not records:
        return 0
    existing = read_ids(path)
    fresh = [r for r in records if r.signal_id not in existing]
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in fresh:
            fh.write(rec.model_dump_json() + "\n")
    return len(fresh)


def actionable_levels(signal) -> tuple[float, float, float, float, str]:
    """The levels a user would actually act on: the refined plan when it is
    actionable, otherwise the daily plan."""
    plan = getattr(signal, "entry_plan", None)
    if plan is not None and plan.mode in ("market", "pullback"):
        return plan.entry, plan.stop, plan.tp1, plan.tp2, plan.mode
    return signal.entry, signal.stop, signal.tp1, signal.tp2, "daily"


def record_for_signal(report, signal, run_id: str, *, market: str,
                      data_hash: str = "", bars_last_date: str = "",
                      demo: bool = False, source: str = "scan",
                      cost_pct: float | None = None) -> LedgerRecord:
    entry, stop, tp1, tp2, mode = actionable_levels(signal)
    risk = entry - stop
    risk_pct = (risk / entry * 100.0) if entry else 0.0
    cost = cost_pct if cost_pct is not None else cost_model.round_trip_cost_pct(market, signal.symbol)
    import signaldesk

    return LedgerRecord(
        signal_id=f"{run_id}:{signal.symbol}",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        as_of=report.as_of,
        run_id=run_id,
        market=market,
        symbol=signal.symbol,
        direction=getattr(signal, "direction", "LONG"),
        score=float(signal.score),
        preset=getattr(report, "scoring_preset", ""),
        weights_hash=scoring.weights_fingerprint(),
        entry=round(float(entry), 8), stop=round(float(stop), 8),
        tp1=round(float(tp1), 8), tp2=round(float(tp2), 8),
        entry_mode=mode,
        risk=round(float(risk), 8), risk_pct=round(float(risk_pct), 6),
        cost_pct=round(float(cost), 4),
        cost_in_r=round(float(cost_model.cost_in_r(cost, risk_pct)), 4),
        data_hash=data_hash, bars_last_date=bars_last_date,
        source=source, demo=demo,
        app_version=getattr(signaldesk, "__version__", ""),
    )


def record_report(report, run_id: str, *, market: str | None = None,
                  data_hashes: dict[str, str] | None = None,
                  bars_last_dates: dict[str, str] | None = None,
                  cost_pcts: dict[str, float] | None = None,
                  demo: bool = False, source: str = "scan",
                  signals: list | None = None) -> list[LedgerRecord]:
    """Ledger records for a report's signals (ScanReport.signals by default)."""
    mkt = market or getattr(report, "market", "")
    data_hashes = data_hashes or {}
    bars_last_dates = bars_last_dates or {}
    cost_pcts = cost_pcts or {}
    items = signals if signals is not None else getattr(report, "signals", [])
    return [
        record_for_signal(report, s, run_id, market=mkt,
                          data_hash=data_hashes.get(s.symbol, ""),
                          bars_last_date=bars_last_dates.get(s.symbol, ""),
                          demo=demo, source=source,
                          cost_pct=cost_pcts.get(s.symbol))
        for s in items
    ]


def backfill(data_dir: Path, *, market: str | None = None) -> int:
    """Import signals from past `runs/*/report.json` (no data hash available).

    Gives `signaldesk outcomes` a historical sample immediately instead of
    waiting weeks for new scans. The resolution anchor is the report date.
    """
    from signaldesk.report.schema import ScanReport

    runs_dir = Path(data_dir) / "runs"
    if not runs_dir.is_dir():
        return 0
    records: list[LedgerRecord] = []
    for report_json in sorted(runs_dir.glob("*/report.json")):
        run_id = report_json.parent.name
        try:
            report = ScanReport(**json.loads(report_json.read_text(encoding="utf-8")))
        except Exception:
            continue
        if market and report.market != market:
            continue
        recs = record_report(
            report, run_id, market=report.market,
            bars_last_dates={s.symbol: report.as_of[:10] for s in report.signals},
            demo=run_id.endswith("-demo"), source="backfill",
        )
        records.extend(recs)
    return append_records(ledger_path(data_dir), records)
