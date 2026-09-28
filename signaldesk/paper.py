"""Paper-trade execution log: what actually happened vs what the model assumed.

The signal ledger freezes the *modelled* plan; this file records the human
(paper) execution side, so the measurement layer can separate "the signal was
good" from "the execution matched the model":

- fill — the signal was taken, optionally with the real entry price. The gap
  between fill and the modelled entry is slippage, expressed in R and
  direction-aware: for a LONG, buying above the modelled entry is a negative
  gap (worse than assumed).
- miss — the signal was not taken (skipped, or the entry never came). Fill
  rate plus the slippage of taken signals is exactly the execution-vs-model
  gap the roadmap asks for; missed signals say nothing about signal quality.

Rules:
- Append-only JSONL at `<data_dir>/paper.jsonl`, one decision per signal:
  statistics use the **first** fill-or-miss recorded per signal_id. Later
  events for the same signal stay in the file but never re-label the decision
  (same spirit as rule R7).
- Outcomes keep resolving the modelled plan from the ledger unchanged. Paper
  events are additive measurement: entry gap, fill rate, and the funding drag
  of held crypto positions (`costs.carry_cost_pct` over the resolved hold).
"""
from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from . import costs as cost_model

PAPER_FILENAME = "paper.jsonl"


class PaperEvent(BaseModel):
    signal_id: str
    event: str                       # fill | miss
    created_at: str
    price: float | None = None       # actual (paper) fill price, for fills
    note: str = ""


def paper_path(data_dir: Path) -> Path:
    return Path(data_dir) / PAPER_FILENAME


def read_events(path: Path) -> list[PaperEvent]:
    path = Path(path)
    if not path.is_file():
        return []
    out: list[PaperEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(PaperEvent(**json.loads(line)))
        except Exception:
            continue
    return out


def first_events(data_dir: Path) -> dict[str, PaperEvent]:
    """The decision per signal: first fill-or-miss in file (append) order."""
    first: dict[str, PaperEvent] = {}
    for ev in read_events(paper_path(data_dir)):
        first.setdefault(ev.signal_id, ev)
    return first


def log_event(data_dir: Path, signal_id: str, event: str, *,
              price: float | None = None, note: str = "") -> PaperEvent:
    """Append one paper decision. Rejects events for signals not in the ledger."""
    from . import ledger as ledger_mod

    if event not in ("fill", "miss"):
        raise ValueError(f"unknown paper event: {event!r}")
    if price is not None and event != "fill":
        raise ValueError("only fills carry a fill price")
    known = ledger_mod.read_ids(ledger_mod.ledger_path(data_dir))
    if signal_id not in known:
        raise KeyError(f"signal_id not in ledger: {signal_id}")
    ev = PaperEvent(signal_id=signal_id, event=event,
                    created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    price=price, note=note)
    path = paper_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(ev.model_dump_json() + "\n")
    return ev


def entry_gap_r(record, price: float) -> float:
    """Fill-vs-model entry gap in R, positive = better than the model."""
    risk = float(getattr(record, "risk", 0.0) or 0.0)
    if risk <= 0 or price <= 0:
        return 0.0
    if str(getattr(record, "direction", "LONG")).upper() == "LONG":
        return round((float(record.entry) - price) / risk, 4)
    return round((price - float(record.entry)) / risk, 4)


def paper_stats(data_dir: Path, records_by_id: dict,
                resolutions_by_id: dict[str, dict]) -> dict:
    """Execution-vs-model gap stats: fill rate, entry slippage, funding drag."""
    first = first_events(data_dir)
    fills = [e for e in first.values() if e.event == "fill"]
    misses = [e for e in first.values() if e.event == "miss"]

    gaps = [entry_gap_r(records_by_id[e.signal_id], e.price)
            for e in fills
            if e.price is not None and e.signal_id in records_by_id]
    funding: list[float] = []
    for ev in fills:
        rec = records_by_id.get(ev.signal_id)
        res = resolutions_by_id.get(ev.signal_id)
        if not rec or not res or res.get("status") not in ("tp1", "tp2", "sl", "time"):
            continue
        carry_pct = cost_model.carry_cost_pct(rec.market, float(res.get("bars") or 0))
        risk_pct = float(rec.risk_pct or 0.0)
        if carry_pct > 0 and risk_pct > 0:
            funding.append(round(carry_pct / risk_pct, 4))

    decided = len(fills) + len(misses)
    stats = {
        "fills": len(fills),
        "misses": len(misses),
        "fill_rate": round(len(fills) / decided, 3) if decided else 0.0,
        "mean_entry_gap_r": round(statistics.fmean(gaps), 3) if gaps else 0.0,
        "n_entry_gap": len(gaps),
        "mean_funding_r": round(statistics.fmean(funding), 3) if funding else 0.0,
        "n_funding": len(funding),
        "notes": [],
    }
    if decided and stats["fill_rate"] < 1.0:
        stats["notes"].append(
            f"fill rate {stats['fill_rate']:.0%}: missed signals mean the realised "
            "sample is the subset you actually took — expectancy from the ledger "
            "still scores every emitted signal")
    if gaps:
        stats["notes"].append(
            f"entry slippage {stats['mean_entry_gap_r']:+.2f}R on average across "
            f"{len(gaps)} priced fill(s): the model assumes a fill at the signal close")
    return stats
