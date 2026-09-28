"""Resolution pipeline shared by the CLI, the API dashboard and the scheduler.

`run_resolution` is the single code path that turns the signal ledger into
persisted resolutions: read records, apply the market/demo filters, fetch
daily bars once per symbol, run the triple-barrier resolver, and append to
outcomes.jsonl. The CLI, `POST /api/outcomes/resolve` and the background
scheduler all go through it — one convention set (rule R7), one place to
change it.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import ledger as ledger_mod
from . import outcomes as outcomes_mod


def select_records(data_dir: Path, *, market: str | None = None, demo: bool = False,
                   include_demo: bool = False, backfill: bool = True,
                   on_backfill: Callable | None = None) -> list:
    """Read the ledger and apply the market/demo/anchor filters.

    Demo records are excluded unless the run itself is demo-based (`demo=True`)
    or explicitly included; records without a decision bar date cannot be
    anchored and are dropped. `backfill=True` imports signals from past
    `runs/*/report.json` first (idempotent: ids dedupe on append); when it
    imports anything, `on_backfill(count)` fires.
    """
    data_dir = Path(data_dir)
    ledger_file = ledger_mod.ledger_path(data_dir)
    if backfill or not ledger_file.is_file():
        imported = ledger_mod.backfill(data_dir, market=market)
        if imported and on_backfill:
            on_backfill(imported)
    records = ledger_mod.read_records(ledger_file)
    if market:
        records = [r for r in records if r.market == market]
    if not include_demo and not demo:
        records = [r for r in records if not r.demo]
    return [r for r in records if r.bars_last_date]


def build_bars_provider(data_dir: Path, market: str, *, demo: bool = False) -> Callable:
    """Daily-bars provider for one market via the yfinance or demo OHLCV tool."""
    bar_dir = Path(data_dir) / "outcome_bars"
    if demo:
        from .tools.demo import DemoOHLCVTool

        return outcomes_mod.bars_provider_from_tool(DemoOHLCVTool(bar_dir, market), period="2y")
    from .tools.yfinance_tools import YFinanceOHLCVTool

    return outcomes_mod.bars_provider_from_tool(YFinanceOHLCVTool(bar_dir, market=market),
                                                period="2y")


def join_rows(records: list, latest: dict[str, dict]) -> list[dict]:
    """Latest resolution per record joined with its entry mode (metrics input)."""
    rows: list[dict] = []
    for rec in records:
        res = latest.get(rec.signal_id)
        if not res:
            continue
        row = dict(res)
        row["mode"] = rec.entry_mode
        row["market"] = rec.market
        rows.append(row)
    return rows


def run_resolution(data_dir: Path, *, records: list | None = None,
                   market: str | None = None, demo: bool = False,
                   include_demo: bool = False, backfill: bool = True,
                   horizon_bars: int = outcomes_mod.RESOLVE_HORIZON_BARS,
                   tool_builder: Callable | None = None,
                   on_progress: Callable | None = None) -> list[dict]:
    """Resolve ledger signals against subsequent daily bars (WF-5).

    Pass `records` to skip re-reading/filtering the ledger (the CLI already
    has them); otherwise they are selected here. Returns the joined rows for
    `metrics.summarize` — an empty list when there is nothing to score.
    """
    data_dir = Path(data_dir)
    if records is None:
        records = select_records(data_dir, market=market, demo=demo,
                                 include_demo=include_demo, backfill=backfill)
    if not records:
        return []

    resolutions: list = []
    for mkt in sorted({r.market for r in records}):
        group = [r for r in records if r.market == mkt]
        if tool_builder is not None:
            provider = outcomes_mod.bars_provider_from_tool(tool_builder(mkt), period="2y")
        else:
            provider = build_bars_provider(data_dir, mkt, demo=demo)
        resolutions += outcomes_mod.resolve_records(
            group, provider, horizon_bars=horizon_bars, on_progress=on_progress)

    outcomes_mod.write_resolutions(outcomes_mod.outcomes_path(data_dir), resolutions)
    latest = outcomes_mod.load_latest(outcomes_mod.outcomes_path(data_dir))
    return join_rows(records, latest)


def signal_rows(data_dir: Path, *, market: str | None = None, include_demo: bool = False,
                limit: int = 200) -> list[dict]:
    """Ledger records joined with their latest known resolution — no fetching.

    This is the dashboard table: newest signals first, each with its current
    outcome status. Open signals surface here for paper fill/miss logging.
    """
    data_dir = Path(data_dir)
    records = ledger_mod.read_records(ledger_mod.ledger_path(data_dir))
    if market:
        records = [r for r in records if r.market == market]
    if not include_demo:
        records = [r for r in records if not r.demo]
    latest = outcomes_mod.load_latest(outcomes_mod.outcomes_path(data_dir))
    rows: list[dict] = []
    for rec in sorted(records, key=lambda r: r.created_at, reverse=True)[:limit]:
        row = rec.model_dump()
        res = latest.get(rec.signal_id) or {}
        row["status"] = res.get("status", "open")
        row["r_net"] = res.get("r_net")
        row["resolved_at"] = res.get("resolved_at", "")
        row["bars"] = res.get("bars", 0)
        rows.append(row)
    return rows
