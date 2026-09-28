"""Universe integrity (roadmap item 29): liquidity screen + survivorship audit.

Two guards for the honesty of the outcome statistics (R7):

- **Liquidity screen** — the movers tools already fetch 24h volume and market
  cap; the scan uses them to keep names a retail account could not actually
  trade out of the universe. Floors are declared assumptions (cited in the
  report like the cost model), applied only where a tape exists (crypto —
  spot FX has none, see `markets.FX_VOLUME_AVAILABLE`). Rows without liquidity
  data are kept and disclosed: a missing tape is not evidence of illiquidity.
- **Point-in-time universe audit** — `runs/*/report.json` preserves each run's
  universe as it looked on the scan date. Classifying those names against
  *today's* bars sizes the selection/survivorship bias in the accumulated
  ledger: dormant and delisted names stop appearing in fresh universes, so
  historical expectancy is measured on survivors and is optimistic until
  enough post-screen signals accumulate.

The audit is read-only measurement: it never re-derives signals from history
(the TAD 3.8 contract — the ledger stays the only source of truth about what
was emitted). Demo runs and demo signals are excluded unless audited with
`--demo`, mirroring the resolver's demo filter.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

# Declared floors, cited in reports (source_tool="liquidity_screen"). Chosen
# conservatively for a retail paper-trading context; the floor that ultimately
# matters is notional volume vs position size, which lands with item 30.
FLOORS: dict[str, dict[str, float]] = {
    "crypto": {"min_24h_volume_usd": 5_000_000.0, "min_market_cap_usd": 50_000_000.0},
}
AUDIT_FILENAME = "universe_audit.json"
DEFAULT_STALE_DAYS = 30   # no fresh bar in 30d -> dormant (still listed, tape died)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if pd.notna(v) else None


def _usd_m(v: float) -> str:
    return f"${v / 1e6:,.1f}M"


def liquidity_screen(df: pd.DataFrame, market: str) -> tuple[pd.DataFrame, list[dict]]:
    """Split a movers frame into (kept, dropped) by the market's floors.

    Markets without floors (FX/metals — no centralized tape) pass through
    untouched. The screen only drops a name when the data *says* it is
    illiquid; rows with missing volume/cap are kept (rule R4).
    """
    floors = FLOORS.get(market)
    if floors is None or df is None or df.empty or "symbol" not in df.columns:
        return df, []
    kept_rows: list[dict] = []
    dropped: list[dict] = []
    for row in df.to_dict("records"):
        reasons: list[str] = []
        volume = _num(row.get("volume"))
        cap = _num(row.get("market_cap"))
        if volume is not None and volume < floors["min_24h_volume_usd"]:
            reasons.append(f"24h volume {_usd_m(volume)} < "
                           f"{_usd_m(floors['min_24h_volume_usd'])} floor")
        if cap is not None and cap < floors["min_market_cap_usd"]:
            reasons.append(f"market cap {_usd_m(cap)} < "
                           f"{_usd_m(floors['min_market_cap_usd'])} floor")
        if reasons:
            dropped.append({"symbol": str(row["symbol"]), "reason": "; ".join(reasons)})
        else:
            kept_rows.append(row)
    return pd.DataFrame(kept_rows, columns=df.columns), dropped


@dataclass
class UniverseAudit:
    runs: int = 0
    first_run: str = ""
    last_run: str = ""
    scanned_symbols: int = 0
    active: int = 0
    dormant: int = 0
    no_data: int = 0
    signal_symbols: int = 0        # distinct ledger symbols in the historical universe
    signal_symbols_gone: int = 0   # of those, no longer active today
    survivorship_exposure: float = 0.0
    stale_days: int = DEFAULT_STALE_DAYS
    audited_at: str = ""
    rows: list[dict] = field(default_factory=list)   # per-symbol detail
    notes: list[str] = field(default_factory=list)


def universe_history(data_dir: Path, *, market: str | None = None,
                     demo: bool = False) -> list[dict]:
    """Point-in-time universes from `runs/*/report.json` (raw keys, schema-light)."""
    runs_dir = Path(data_dir) / "runs"
    out: list[dict] = []
    for report_json in sorted(runs_dir.glob("*/report.json")):
        run_id = report_json.parent.name
        if not demo and run_id.endswith("-demo"):
            continue
        try:
            data = json.loads(report_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        mkt = str(data.get("market", "") or "")
        if market and mkt != market:
            continue
        syms = data.get("universe") or []
        if not isinstance(syms, list) or not syms:
            continue
        out.append({"run_id": run_id, "market": mkt, "as_of": str(data.get("as_of", "")),
                    "symbols": [str(s) for s in syms]})
    return out


def _signal_map(data_dir: Path, *, market: str | None = None,
                demo: bool = False) -> dict[tuple[str, str], int]:
    from . import ledger as ledger_mod

    counts: dict[tuple[str, str], int] = {}
    for rec in ledger_mod.read_records(ledger_mod.ledger_path(data_dir)):
        if market and rec.market != market:
            continue
        if rec.demo and not demo:
            continue
        key = (rec.market, rec.symbol)
        counts[key] = counts.get(key, 0) + 1
    return counts


def audit_universe(history: list[dict], signals: dict[tuple[str, str], int],
                   providers: dict[str, Callable[[str], pd.DataFrame]], *,
                   stale_days: int = DEFAULT_STALE_DAYS, today: date | None = None,
                   on_progress: Callable[[str], None] | None = None) -> UniverseAudit:
    """Classify each historical (market, symbol) against today's bars.

    `providers` maps market -> symbol -> daily-bars DataFrame. `run_audit`
    builds them from the resolver's OHLCV tools; tests inject stubs.
    """
    today = today or datetime.now(UTC).date()
    cutoff = (today - timedelta(days=stale_days)).isoformat()
    audit = UniverseAudit(runs=len(history), stale_days=stale_days,
                          audited_at=datetime.now(UTC).isoformat(timespec="seconds"))
    stamps = sorted(h["as_of"][:10] for h in history if h["as_of"])
    if stamps:
        audit.first_run, audit.last_run = stamps[0], stamps[-1]

    seen: dict[tuple[str, str], dict] = {}
    for h in history:
        for sym in h["symbols"]:
            as_of = h["as_of"][:10]
            entry = seen.setdefault((h["market"], sym), {
                "market": h["market"], "symbol": sym,
                "first_seen": as_of, "last_seen": as_of, "runs": 0})
            entry["runs"] += 1
            entry["first_seen"] = min(entry["first_seen"], as_of)
            entry["last_seen"] = max(entry["last_seen"], as_of)

    audit.scanned_symbols = len(seen)
    signal_symbols = 0
    gone_signal_symbols = 0
    for (mkt, sym), entry in sorted(seen.items()):
        n_signals = signals.get((mkt, sym), 0)
        if n_signals:
            signal_symbols += 1
        last_bar = ""
        provider = providers.get(mkt)
        if provider is not None:
            try:
                frame = provider(sym)
            except Exception:
                frame = pd.DataFrame()
            if frame is not None and not frame.empty and "date" in frame.columns:
                try:
                    last_bar = str(pd.to_datetime(frame["date"]).max().date())
                except Exception:
                    last_bar = ""
        if not last_bar:
            status = "no_data"
        elif last_bar >= cutoff:
            status = "active"
        else:
            status = "dormant"
        if n_signals and status != "active":
            gone_signal_symbols += 1
        audit.rows.append({**entry, "signals": n_signals, "status": status,
                           "last_bar": last_bar})
        if on_progress:
            on_progress(f"{mkt} {sym}: {status}")

    audit.active = sum(1 for r in audit.rows if r["status"] == "active")
    audit.dormant = sum(1 for r in audit.rows if r["status"] == "dormant")
    audit.no_data = sum(1 for r in audit.rows if r["status"] == "no_data")
    audit.signal_symbols = signal_symbols
    audit.signal_symbols_gone = gone_signal_symbols
    audit.survivorship_exposure = (round(gone_signal_symbols / signal_symbols, 3)
                                   if signal_symbols else 0.0)
    if audit.no_data:
        audit.notes.append(f"{audit.no_data} name(s) returned no bars — delisted, or the "
                           "data source has nothing for them; either way they cannot emit signals")
    if signal_symbols and gone_signal_symbols:
        audit.notes.append("dormant/delisted names stop appearing in fresh universes: the "
                           "accumulated ledger is survivor-selected and its expectancy is "
                           "optimistic until enough post-screen signals accumulate")
    if not signal_symbols:
        audit.notes.append("no ledger signals yet — the exposure figure becomes meaningful "
                           "once signals accumulate")
    return audit


def run_audit(data_dir: Path, *, market: str | None = None, demo: bool = False,
              stale_days: int = DEFAULT_STALE_DAYS, today: date | None = None,
              providers: dict | None = None,
              on_progress: Callable[[str], None] | None = None) -> UniverseAudit:
    """Audit a data dir's run history + ledger (the CLI entry point)."""
    history = universe_history(data_dir, market=market, demo=demo)
    signals = _signal_map(data_dir, market=market, demo=demo)
    if providers is None:
        from . import resolver as resolver_mod

        providers = {}
        for mkt in {h["market"] for h in history} | {m for m, _ in signals}:
            if mkt:
                providers[mkt] = resolver_mod.build_bars_provider(data_dir, mkt, demo=demo)
    return audit_universe(history, signals, providers, stale_days=stale_days,
                          today=today, on_progress=on_progress)


def audit_path(data_dir: Path) -> Path:
    return Path(data_dir) / AUDIT_FILENAME


def save_audit(data_dir: Path, audit: UniverseAudit) -> Path:
    path = audit_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(audit), default=str, indent=2), encoding="utf-8")
    return path


def load_audit(data_dir: Path) -> dict | None:
    path = audit_path(data_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def bias_note(data_dir: Path) -> str | None:
    """One-line survivorship caveat for the outcome statistics (R7), or None.

    Reads the cached audit (`signaldesk universe` writes it) — never fetches.
    """
    data = load_audit(data_dir)
    if not data:
        return None
    scanned = int(data.get("scanned_symbols") or 0)
    gone_scanned = int(data.get("dormant") or 0) + int(data.get("no_data") or 0)
    signal_symbols = int(data.get("signal_symbols") or 0)
    gone = int(data.get("signal_symbols_gone") or 0)
    if not signal_symbols:
        return None
    when = str(data.get("audited_at", ""))[:10]
    return (f"universe audit {when}: {gone_scanned}/{scanned} scanned names dormant/delisted, "
            f"{gone}/{signal_symbols} signal symbols affected — historical expectancy is "
            "survivor-biased until enough post-screen signals accumulate")


def render_audit_text(a: UniverseAudit) -> str:
    lines = [f"universe audit (point-in-time, {a.runs} run(s), "
             f"{a.first_run or 'n/a'}..{a.last_run or 'n/a'}):"]
    lines.append(f"  {a.scanned_symbols} scanned symbols: {a.active} active · "
                 f"{a.dormant} dormant · {a.no_data} without data "
                 f"(stale > {a.stale_days}d)")
    if a.signal_symbols:
        lines.append(f"  signal symbols: {a.signal_symbols_gone}/{a.signal_symbols} no longer "
                     f"active (survivorship exposure {a.survivorship_exposure:.0%})")
    else:
        lines.append("  no ledger signals yet — exposure undefined")
    for note in a.notes:
        lines.append(f"note: {note}")
    gone = [r for r in a.rows if r["status"] != "active"]
    for r in gone[:20]:
        lines.append(f"  {r['market']:8} {r['symbol']:12} {r['status']:8} "
                     f"last bar {r['last_bar'] or 'never'} · "
                     f"scanned {r['first_seen']}..{r['last_seen']} · signals {r['signals']}")
    if len(gone) > 20:
        lines.append(f"  … and {len(gone) - 20} more")
    return "\n".join(lines)
