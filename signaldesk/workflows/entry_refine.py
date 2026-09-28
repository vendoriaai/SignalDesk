"""Phase 7.5 — intraday entry refinement (WF-1/2/3/4).

Runs after the daily-timeframe scan has ranked candidates and built signals:
fetch lower timeframes (default 30m/15m/5m/1m) for just those symbols,
recompute features in a sandbox pass, and refine entry/stop/TP via the
deterministic rules in scoring.refine_entry_plan. Every number asserted in
the report is registered as a citation (R1); any unavailable timeframe
degrades with a disclosure (R4). The daily ranking itself is never changed.

Stops may tighten only down to the risk floor — max(0.75 x ATR(1d), 15 x the
assumed round-trip cost). The intraday read tunes entry timing; it does not
shrink the risk unit to a size where trading costs consume the R multiple.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

from signaldesk.agent.events import EventBus, EventKind
from signaldesk.citations.registry import CitationRegistry
from signaldesk.markets import entry_tf_period
from signaldesk.report.schema import EntryPlan, Signal
from signaldesk.sandbox.executor import run_script
from signaldesk.strategy import scoring

_ENTRY_SANDBOX_SCRIPT = """\
import json
from pathlib import Path

import pandas as pd

from signaldesk.analysis.indicators import compute_features

out = {}
for p in sorted(Path("input").glob("ohlcv_*.csv")):
    stem = p.stem[len("ohlcv_"):]
    symbol, tf = stem.rsplit("_", 1)
    try:
        feats = compute_features(pd.read_csv(p))
        out.setdefault(symbol, {})[tf] = {
            k: (None if v != v else v) for k, v in feats.items()
            if isinstance(v, (int, float))
        }
    except Exception as exc:
        out.setdefault(symbol, {})[tf] = {"error": str(exc)}
print(json.dumps(out))
"""

# features asserted in the report snapshot — each gets a derived citation
_FEATURE_FORMULAS = {
    "rsi14": "RSI(close,14) Wilder",
    "ema9": "EMA9(close)",
    "ema21": "EMA21(close)",
    "macd_hist": "EMA12-EMA26 minus signal(9)",
    "atr14": "ATR(H,L,C,14) Wilder",
    "swing_low_20": "rolling min(low, 20)",
}


def _cost_in_r(cost_pct: float, risk_pct: float) -> float:
    """Cost-to-risk ratio, safe for zero/absent risk (0.0 instead of inf)."""
    if risk_pct <= 0 or cost_pct <= 0:
        return 0.0
    return round(cost_pct / risk_pct, 4)


def refine_signal_entries(
    signals: list[Signal],
    timeframes: list[str],
    ohlcv,  # .run(symbol=, interval=, period=) -> ToolResult
    run_dir: Path,
    bus: EventBus,
    registry: CitationRegistry,
    disclosures: list[str],
    charts: dict[str, dict[str, str]],
    *,
    price_cite_ids: dict[str, str] | None = None,
    atr_cite_ids: dict[str, str] | None = None,
    atr_daily: dict[str, float] | None = None,
    cost_pcts: dict[str, float] | None = None,
    cost_cite_ids: dict[str, str] | None = None,
) -> dict[str, EntryPlan]:
    """Refine every signal in place (entry_plan + citations) and return the
    plans keyed by symbol. `charts` is mutated with "{tf}-entry" chart keys.

    `atr_daily` and `cost_pcts` feed the risk floor (scoring.min_risk_distance):
    a refined stop may not sit closer than max(0.75 x ATR(1d), 15 x cost), so the
    intraday read tunes the entry without shrinking the risk unit to a size where
    trading costs consume the R multiple. When the floor binds, the plan is
    flagged `risk_floored` and the stop citation says so.
    """
    if not signals or not timeframes:
        return {}

    price_cite_ids = price_cite_ids or {}
    atr_cite_ids = atr_cite_ids or {}
    atr_daily = atr_daily or {}
    cost_pcts = cost_pcts or {}
    cost_cite_ids = cost_cite_ids or {}
    bus.emit("P7.5", EventKind.PHASE,
             f"entry refinement: {', '.join(timeframes)} for {len(signals)} signal(s)")

    fetched: dict[str, dict[str, Path]] = {}
    for sig in signals:
        for tf in timeframes:
            try:
                res = ohlcv.run(symbol=sig.symbol, interval=tf, period=entry_tf_period(tf))
                bus.emit("P7.5", EventKind.TOOL, f"ohlcv: {res.summary}")
                fetched.setdefault(sig.symbol, {})[tf] = res.csv_files[0]
            except Exception as exc:
                disclosures.append(f"{sig.symbol}: no {tf} bars for entry refinement ({exc})")
    if not fetched:
        return {}

    sandbox_dir = run_dir / "sandbox_entry"
    input_dir = sandbox_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    for paths in fetched.values():
        for path in paths.values():
            shutil.copy(path, input_dir / path.name)
    proc = run_script(_ENTRY_SANDBOX_SCRIPT, sandbox_dir)
    bus.emit("P7.5", EventKind.SANDBOX,
             "intraday features: " + proc.stdout.strip()[:160])
    try:
        all_feats = json.loads(proc.stdout.strip())
    except Exception:
        disclosures.append("Entry refinement: sandbox output unreadable; daily plans unchanged.")
        return {}

    plans: dict[str, EntryPlan] = {}
    for sig in signals:
        sym_feats = all_feats.get(sig.symbol) or {}
        ltf = {tf: f for tf, f in sym_feats.items() if isinstance(f, dict) and "error" not in f}
        for tf, f in sym_feats.items():
            if isinstance(f, dict) and "error" in f:
                disclosures.append(f"{sig.symbol}: {tf} features failed ({f['error']})")
        if not ltf:
            continue
        daily = scoring.TradePlan(entry=sig.entry, stop=sig.stop, tp1=sig.tp1, tp2=sig.tp2)
        raw = scoring.refine_entry_plan(daily, ltf, atr_daily=atr_daily.get(sig.symbol),
                                        cost_pct=cost_pcts.get(sig.symbol))
        plan = _register_plan(sig.symbol, raw, ltf, fetched[sig.symbol], registry,
                              price_cite_ids.get(sig.symbol), atr_cite_ids.get(sig.symbol),
                              cost_pct=cost_pcts.get(sig.symbol, 0.0),
                              cost_cite_id=cost_cite_ids.get(sig.symbol))
        plans[sig.symbol] = plan
        sig.entry_plan = plan
        sig.confluence.append(f"entry refined on {', '.join(sorted(plan.timeframes))} ({plan.mode})")
        for cid in plan.citations:
            if cid not in sig.citations:
                sig.citations.append(cid)
        _render_chart(sig, plan, fetched[sig.symbol], run_dir, bus, disclosures, charts)
    return plans


def _register_plan(symbol: str, raw: scoring.EntryPlan, ltf: dict[str, dict],
                   paths: dict[str, Path], registry: CitationRegistry,
                   price_cite_id: str | None, atr_cite_id: str | None,
                   cost_pct: float = 0.0, cost_cite_id: str | None = None) -> EntryPlan:
    """Register direct + derived citations for every number the plan asserts."""
    close_cites: dict[str, str] = {}
    for tf, path in paths.items():
        f = ltf[tf]
        if not scoring._finite(f.get("close")):
            continue
        last_date = ""
        try:
            last_date = str(pd.read_csv(path)["date"].iloc[-1])
        except Exception:
            pass
        cite = registry.register_direct(
            round(float(f["close"]), 8), f"{symbol} {tf} close",
            source_tool="ohlcv", file=Path(path).name,
            row_key=last_date, column="close",
        )
        close_cites[tf] = cite.id

    feat_cites: dict[tuple[str, str], str] = {}
    for tf, f in ltf.items():
        for key, formula in _FEATURE_FORMULAS.items():
            value = f.get(key)
            if tf in close_cites and scoring._finite(value):
                cite = registry.register_derived(
                    round(float(value), 8), f"{symbol} {tf} {key}",
                    formula=f"{formula} on {tf}", derived_from=[close_cites[tf]],
                )
                feat_cites[(tf, key)] = cite.id

    snapshot = {
        tf: {k: round(float(f[k]), 8) for k in _FEATURE_FORMULAS
             if (tf, k) in feat_cites}
        for tf in ltf
    }
    snapshot = {tf: vals for tf, vals in snapshot.items() if vals}

    if raw.mode == "wait":
        # the daily plan stands: the cost-to-risk ratio is the daily one
        daily_pct = (raw.risk_daily / raw.entry * 100.0) if raw.entry else 0.0
        return EntryPlan(
            mode=raw.mode, entry=raw.entry, stop=raw.stop, tp1=raw.tp1, tp2=raw.tp2,
            risk_daily=round(raw.risk_daily, 8), risk_refined=round(raw.risk_refined, 8),
            note=raw.note, roles=raw.roles, timeframes=snapshot,
            citations=[cid for (_tf, _k), cid in sorted(feat_cites.items())],
            cost_pct=round(cost_pct, 4),
            cost_in_r=_cost_in_r(cost_pct, daily_pct),
        )

    # plan-number citations: entry, stop, tp1, tp2
    mid = raw.roles.get("bias", "")
    struct = raw.roles.get("structure", mid)
    entry_derived = [c for c in (
        price_cite_id if raw.mode == "market" else None,
        feat_cites.get((mid, "ema21")), feat_cites.get((mid, "atr14")),
    ) if c]
    entry_formula = ("current close (daily entry kept)" if raw.mode == "market"
                     else f"EMA21({mid}) pullback-zone entry")
    entry_cite = registry.register_derived(
        round(raw.entry, 8), f"{symbol} refined entry ({raw.mode})",
        formula=entry_formula, derived_from=entry_derived or list(close_cites.values()),
    )
    stop_formula = (f"max(daily stop, entry-{scoring.STOP_ATR_MULT:g}*ATR({struct}))")
    if raw.floored:
        stop_formula += (f"; floored at max({scoring.MIN_RISK_ATR_MULT:g}*ATR(1d), "
                         f"{scoring.MIN_RISK_COST_MULT:g}*round-trip cost)")
    stop_derived = [c for c in (
        feat_cites.get((struct, "atr14")), atr_cite_id, cost_cite_id,
    ) if c]
    stop_cite = registry.register_derived(
        round(raw.stop, 8), f"{symbol} refined stop",
        formula=stop_formula, derived_from=stop_derived or list(close_cites.values()),
    )
    tp1_cite = registry.register_derived(
        round(raw.tp1, 8), f"{symbol} TP1 (refined)",
        formula="entry + 2*(entry - stop)", derived_from=[entry_cite.id, stop_cite.id],
    )
    tp2_cite = registry.register_derived(
        round(raw.tp2, 8), f"{symbol} TP2 (refined)",
        formula="entry + 3*(entry - stop)", derived_from=[entry_cite.id, stop_cite.id],
    )
    plan_cites = [entry_cite.id, stop_cite.id, tp1_cite.id, tp2_cite.id]
    plan_cites += [cid for (tf, _), cid in sorted(feat_cites.items()) if tf in snapshot]

    return EntryPlan(
        mode=raw.mode, entry=round(raw.entry, 8), stop=round(raw.stop, 8),
        tp1=round(raw.tp1, 8), tp2=round(raw.tp2, 8),
        risk_daily=round(raw.risk_daily, 8), risk_refined=round(raw.risk_refined, 8),
        note=raw.note, roles=raw.roles, timeframes=snapshot,
        citations=plan_cites,
        cost_pct=round(cost_pct, 4),
        cost_in_r=_cost_in_r(cost_pct, (raw.risk_refined / raw.entry * 100.0) if raw.entry else 0.0),
        risk_floored=bool(raw.floored),
    )


def _render_chart(sig: Signal, plan: EntryPlan, paths: dict[str, Path],
                  run_dir: Path, bus: EventBus, disclosures: list[str],
                  charts: dict[str, dict[str, str]]) -> None:
    from signaldesk.analysis.charts import render_entry_chart

    tf = "15m" if "15m" in paths else next(iter(paths))
    out_dir = run_dir / "sandbox" / "output" / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{sig.symbol}-{tf}-entry.png"
    try:
        df = pd.read_csv(paths[tf])
        render_entry_chart(
            df, sig.symbol, out, title_suffix=f"({tf})",
            # wait mode = watch the level, don't draw an actionable plan
            levels=(None if plan.mode == "wait" else
                    {"entry": plan.entry, "stop": plan.stop,
                     "TP1": plan.tp1, "TP2": plan.tp2}),
        )
        rel = str(Path("sandbox") / "output" / "charts" / out.name).replace("\\", "/")
        charts.setdefault(sig.symbol, {})[f"{tf}-entry"] = rel
        bus.emit("P7.5", EventKind.CHART, f"entry chart ready: {sig.symbol} ({tf})",
                 path=rel, symbol=sig.symbol, tf=f"{tf}-entry")
    except Exception as exc:
        disclosures.append(f"{sig.symbol}: entry chart render failed ({exc})")
