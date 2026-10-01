"""Phase 7.5 — intraday entry refinement (WF-1/2/3/4) + Phase 7.6 — AI chart read.

Phase 7.5 runs after the daily-timeframe scan has ranked candidates and built
signals: fetch lower timeframes (default 30m/15m/5m/1m) for just those symbols,
recompute features in a sandbox pass, and refine entry/stop/TP via the
deterministic rules in scoring.refine_entry_plan. Every number asserted in
the report is registered as a citation (R1); any unavailable timeframe
degrades with a disclosure (R4). The daily ranking itself is never changed.

Stops may tighten only down to the risk floor — max(0.75 x ATR(1d), 15 x the
assumed round-trip cost). The intraday read tunes entry timing; it does not
shrink the risk unit to a size where trading costs consume the R multiple.

Phase 7.6 (`apply_ai_chart_reads`) renders the FULL chart ladder for every
chosen pair (1d -> 1h -> 30m -> 15m -> 5m -> 1m, streamed as chart events
before the report), then one vision-capable LLM call per signal reads the
ladder and chooses the entry. The stop distance stays deterministic —
`scoring.reconcile_ai_entry` clamps it into the risk floor — and the ledger
records entry_mode="ai_chart_v1" so outcomes stay attributable (R7). The
first real (non-demo) application pre-registers trial `ai-chart-entry-v1`
with criteria frozen before any AI-entry outcome exists.
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

# chart-ladder order sent to the vision model (daily first)
_LADDER_TFS = ("1d", "1h", "30m", "15m", "5m", "1m")

_TRIAL_NAME = "ai-chart-entry-v1"


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
    fetched_out: dict[str, dict[str, Path]] | None = None,
) -> dict[str, EntryPlan]:
    """Refine every signal in place (entry_plan + citations) and return the
    plans keyed by symbol. `charts` is mutated with "{tf}-entry" chart keys —
    one chart per fetched timeframe, not just the primary. `fetched_out`, when
    given, receives {symbol: {tf: csv path}} so Phase 7.6 can re-render the
    final plan onto a chart.

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
    if fetched_out is not None:
        fetched_out.update(fetched)

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


def fetch_ltf_charts(
    signals: list[Signal],
    timeframes: list[str],
    ohlcv,
    run_dir: Path,
    bus: EventBus,
    disclosures: list[str],
    charts: dict[str, dict[str, str]],
) -> dict[str, dict[str, Path]]:
    """Fetch intraday bars + render entry-style charts for signals the
    deterministic refinement did not cover (SHORT signals today): the Phase 7.6
    vision read needs the full chart ladder for every chosen pair, long or
    short. Charts render with no level lines (no plan exists yet)."""
    fetched: dict[str, dict[str, Path]] = {}
    for sig in signals:
        own = charts.get(sig.symbol) or {}
        todo = [tf for tf in timeframes
                if f"{tf}-entry" not in own and tf not in own]
        if not todo:
            continue
        bus.emit("P7.5", EventKind.PHASE,
                 f"chart ladder: {sig.symbol} ({sig.direction}) {', '.join(todo)}")
        for tf in todo:
            try:
                res = ohlcv.run(symbol=sig.symbol, interval=tf, period=entry_tf_period(tf))
                bus.emit("P7.5", EventKind.TOOL, f"ohlcv: {res.summary}")
                fetched.setdefault(sig.symbol, {})[tf] = res.csv_files[0]
            except Exception as exc:
                disclosures.append(f"{sig.symbol}: no {tf} bars ({exc})")
    for sym, paths in fetched.items():
        for tf, path in paths.items():
            _render_one_chart(sym, tf, path, run_dir, bus, disclosures, charts,
                              levels=None, note="chart ladder")
    return fetched


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


def _render_one_chart(symbol: str, tf: str, csv_path: Path, run_dir: Path,
                      bus: EventBus, disclosures: list[str],
                      charts: dict[str, dict[str, str]],
                      levels: dict[str, float] | None, note: str = "") -> None:
    """Render one entry-style chart for (symbol, tf) and stream the chart event."""
    from signaldesk.analysis.charts import render_entry_chart

    out_dir = run_dir / "sandbox" / "output" / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{symbol}-{tf}-entry.png"
    try:
        df = pd.read_csv(csv_path)
        render_entry_chart(df, symbol, out, title_suffix=f"({tf})", levels=levels)
        rel = str(Path("sandbox") / "output" / "charts" / out.name).replace("\\", "/")
        charts.setdefault(symbol, {})[f"{tf}-entry"] = rel
        bus.emit("P7.5", EventKind.CHART, f"entry chart ready: {symbol} ({tf})",
                 path=rel, symbol=symbol, tf=f"{tf}-entry")
    except Exception as exc:
        disclosures.append(f"{symbol}: entry chart render failed ({note} {tf}) ({exc})")


def _render_chart(sig: Signal, plan: EntryPlan, paths: dict[str, Path],
                  run_dir: Path, bus: EventBus, disclosures: list[str],
                  charts: dict[str, dict[str, str]]) -> None:
    """Render an entry chart for EVERY fetched timeframe (Phase 7.6: the vision
    read and the UI gallery need the full ladder, not just one chart)."""
    for tf, path in paths.items():
        # wait mode = watch the level, don't draw an actionable plan
        levels = (None if plan.mode == "wait" else
                  {"entry": plan.entry, "stop": plan.stop,
                   "TP1": plan.tp1, "TP2": plan.tp2})
        _render_one_chart(sig.symbol, tf, path, run_dir, bus, disclosures,
                          charts, levels=levels)


def _ladder_paths(symbol: str, charts: dict[str, dict[str, str]],
                  run_dir: Path) -> list[tuple[str, Path]]:
    """Ordered (tf, png path) chart ladder for one symbol, daily first.
    Prefers the "-entry" render when a timeframe has both."""
    own = charts.get(symbol) or {}
    ladder: list[tuple[str, Path]] = []
    for tf in _LADDER_TFS:
        for key in (f"{tf}-entry", tf):
            rel = own.get(key)
            if not rel:
                continue
            path = run_dir / rel
            if path.exists():
                ladder.append((key, path))
                break
    return ladder


def maybe_ai_chart_reads(
    signals: list[Signal],
    charts: dict[str, dict[str, str]],
    run_dir: Path,
    bus: EventBus,
    registry: CitationRegistry,
    disclosures: list[str],
    *,
    demo_mode: bool,
    price_cite_ids: dict[str, str] | None = None,
    atr_cite_ids: dict[str, str] | None = None,
    atr_daily: dict[str, float] | None = None,
    cost_pcts: dict[str, float] | None = None,
    cost_cite_ids: dict[str, str] | None = None,
    ltf_csvs: dict[str, dict[str, Path]] | None = None,
    context: str | None = None,
) -> int:
    """Phase 7.6 guard + entry point: settings toggle, demo skip, creds, then
    `apply_ai_chart_reads`. Returns the number of signals whose entry was
    chosen by the AI read."""
    if not signals:
        return 0
    try:
        data_dir = run_dir.parent.parent
        from signaldesk import userconfig

        if not userconfig.Settings(data_dir).get("ai_chart_entry"):
            return 0
    except Exception:
        pass
    if demo_mode:
        disclosures.append("Demo scan: AI chart read skipped (offline, deterministic).")
        return 0
    from signaldesk.agent import vision as vision_mod

    creds = vision_mod.resolve_creds()
    if creds is None:
        disclosures.append(
            "AI chart read unavailable: no LLM key configured — deterministic "
            "entry plans kept (R4).")
        return 0
    provider, key, model = creds
    model_label = vision_mod.model_id(provider, model)
    bus.emit("P7.6", EventKind.PHASE,
             f"AI chart read: {len(signals)} signal(s), chart ladder 1d -> 1m (model {model_label})")
    try:
        applied = apply_ai_chart_reads(
            signals, charts, run_dir, bus, registry, disclosures,
            provider=provider, key=key, model=model, model_label=model_label,
            price_cite_ids=price_cite_ids, atr_cite_ids=atr_cite_ids,
            atr_daily=atr_daily, cost_pcts=cost_pcts, cost_cite_ids=cost_cite_ids,
            ltf_csvs=ltf_csvs, context=context)
    except Exception as exc:
        disclosures.append(f"AI chart read failed: {exc} — deterministic plans kept (R4).")
        return 0
    if applied:
        _ensure_trial(run_dir.parent.parent)
    return applied


def apply_ai_chart_reads(
    signals: list[Signal],
    charts: dict[str, dict[str, str]],
    run_dir: Path,
    bus: EventBus,
    registry: CitationRegistry,
    disclosures: list[str],
    *,
    provider: str,
    key: str,
    model: str | None,
    model_label: str,
    price_cite_ids: dict[str, str] | None = None,
    atr_cite_ids: dict[str, str] | None = None,
    atr_daily: dict[str, float] | None = None,
    cost_pcts: dict[str, float] | None = None,
    cost_cite_ids: dict[str, str] | None = None,
    ltf_csvs: dict[str, dict[str, Path]] | None = None,
    context: str | None = None,
) -> int:
    """One vision call per signal over the full chart ladder; the chosen entry
    replaces the deterministic level after `scoring.reconcile_ai_entry` clamps
    the stop geometry. Every asserted number is cited (R1); the read itself is
    streamed as ANALYSIS events so the trace shows the AI's chart read before
    the report emits the signals. `context` carries the scan's market context
    (news, macro, regime) so the entry decision sees it too."""
    from signaldesk.agent import vision as vision_mod

    price_cite_ids = price_cite_ids or {}
    atr_cite_ids = atr_cite_ids or {}
    atr_daily = atr_daily or {}
    cost_pcts = cost_pcts or {}
    cost_cite_ids = cost_cite_ids or {}
    ltf_csvs = ltf_csvs or {}

    applied = 0
    for sig in signals:
        ladder = _ladder_paths(sig.symbol, charts, run_dir)
        if len(ladder) < 2:
            disclosures.append(
                f"{sig.symbol}: AI chart read skipped — chart ladder incomplete "
                f"({len(ladder)} chart(s) usable).")
            continue
        draft = scoring.TradePlan(entry=sig.entry, stop=sig.stop,
                                  tp1=sig.tp1, tp2=sig.tp2)
        prior_plan = sig.entry_plan
        draft_mode = (prior_plan.mode if prior_plan is not None
                      and prior_plan.mode in ("market", "pullback") else "market")
        brief = (
            f"Deterministic draft plan: entry {sig.entry:.8g}, stop {sig.stop:.8g}, "
            f"TP1 {sig.tp1:.8g}, TP2 {sig.tp2:.8g}."
        )
        if sig.direction == "SHORT":
            brief += " The direction is SHORT: enter on strength into resistance, stop above."
        else:
            brief += " The direction is LONG: prefer pullback entries toward support/EMA zones."
        if getattr(sig, "catalysts", None):
            news_items = [c.split(" [")[0] for c in sig.catalysts[:3]]
            brief += "\nsymbol news: " + " | ".join(news_items)
        if context:
            brief += "\n--- market context ---\n" + context
        bus.emit("P7.6", EventKind.ANALYSIS,
                 f"watching {sig.symbol} {sig.direction}: {len(ladder)} charts "
                 f"({', '.join(k for k, _ in ladder)})",
                 symbol=sig.symbol, direction=sig.direction,
                 charts=[k for k, _ in ladder], model=model_label)
        errors: list[str] = []
        read = vision_mod.chart_entry_read(
            sig.symbol, sig.direction, chart_pngs=ladder, brief=brief,
            provider=provider, key=key, model=model, errors=errors)
        if read is None:
            why = f" ({errors[0]})" if errors else ""
            disclosures.append(
                f"{sig.symbol}: AI chart read unavailable — deterministic plan "
                f"kept (R4){why}.")
            bus.emit("P7.6", EventKind.WARN,
                     f"AI chart read failed for {sig.symbol}{why}")
            continue
        plan = scoring.reconcile_ai_entry(
            draft, direction=sig.direction, ai_entry=read["entry"],
            ai_stop=read["stop"], atr_daily=atr_daily.get(sig.symbol),
            cost_pct=cost_pcts.get(sig.symbol), mode=draft_mode,
            note=read["rationale"])
        if plan is None:
            disclosures.append(
                f"{sig.symbol}: AI entry implausible vs the daily plan — "
                "deterministic entry kept (R4).")
            bus.emit("P7.6", EventKind.WARN,
                     f"AI entry rejected for {sig.symbol} (implausible level)")
            continue

        for tf, tf_read in read["reads"].items():
            bus.emit("P7.6", EventKind.ANALYSIS,
                     f"{sig.symbol} {tf}: {tf_read['trend'] or 'n/a'} — {tf_read['note']}"
                     if tf_read["note"] else f"{sig.symbol} {tf}: {tf_read['trend'] or 'n/a'}",
                     symbol=sig.symbol, tf=tf,
                     trend=tf_read["trend"] or "n/a", note=tf_read["note"])

        cites = _register_ai_plan(sig, plan, read, model_label, registry,
                                  price_cite_ids.get(sig.symbol),
                                  atr_cite_ids.get(sig.symbol),
                                  cost_pct=cost_pcts.get(sig.symbol, 0.0),
                                  cost_cite_id=cost_cite_ids.get(sig.symbol))
        sig.entry = round(plan.entry, 8)
        sig.stop = round(plan.stop, 8)
        sig.tp1 = round(plan.tp1, 8)
        sig.tp2 = round(plan.tp2, 8)
        sig.entry_plan = EntryPlan(
            mode=plan.mode, entry=round(plan.entry, 8), stop=round(plan.stop, 8),
            tp1=round(plan.tp1, 8), tp2=round(plan.tp2, 8),
            risk_daily=round(plan.risk_daily, 8), risk_refined=round(plan.risk_refined, 8),
            note=plan.note,
            roles=(prior_plan.roles if prior_plan is not None else {}),
            timeframes=(prior_plan.timeframes if prior_plan is not None else {}),
            citations=cites,
            cost_pct=round(cost_pcts.get(sig.symbol, 0.0), 4),
            cost_in_r=_cost_in_r(cost_pcts.get(sig.symbol, 0.0),
                                 (plan.risk_refined / plan.entry * 100.0) if plan.entry else 0.0),
            risk_floored=bool(plan.floored),
            ai_entry=True, ai_model=model_label,
            ai_rationale=read["rationale"], ai_confidence=read["confidence"],
            ai_reads=read["reads"],
        )
        sig.confluence.append(
            f"entry chosen by AI chart read on {', '.join(k for k, _ in ladder)} "
            f"({model_label})")
        bus.emit("P7.6", EventKind.ANALYSIS,
                 f"{sig.symbol} entry -> {sig.entry:.8g} (stop {sig.stop:.8g}, "
                 f"TP1 {sig.tp1:.8g}, TP2 {sig.tp2:.8g}, confidence "
                 f"{read['confidence']:.2f}): {read['rationale']}",
                 symbol=sig.symbol, direction=sig.direction,
                 entry=sig.entry, stop=sig.stop, tp1=sig.tp1, tp2=sig.tp2,
                 confidence=read["confidence"], rationale=read["rationale"],
                 reads=read["reads"], model=model_label)
        _rerender_final_chart(sig, ltf_csvs.get(sig.symbol) or {}, run_dir, bus,
                              disclosures, charts)
        applied += 1
    return applied


def _register_ai_plan(sig: Signal, plan: scoring.EntryPlan, read: dict,
                      model_label: str, registry: CitationRegistry,
                      price_cite_id: str | None, atr_cite_id: str | None,
                      cost_pct: float = 0.0, cost_cite_id: str | None = None) -> list[str]:
    """Citations for every number the AI plan asserts (R1): plan levels as
    derived figures, the read itself as a direct llm_vision source."""
    derived_from = [c for c in (price_cite_id, atr_cite_id, cost_cite_id) if c]
    floor_txt = (f"stop distance clamped to [max({scoring.MIN_RISK_ATR_MULT:g}*ATR(1d), "
                 f"{scoring.MIN_RISK_COST_MULT:g}*cost), 3*ATR(1d)]")

    def _plan_cite(value: float, label: str, formula: str, from_ids: list[str],
                   column: str):
        if from_ids:
            return registry.register_derived(value, label, formula=formula,
                                             derived_from=from_ids)
        return registry.register_direct(value, label, source_tool="llm_vision",
                                        column=column)

    entry_cite = _plan_cite(round(plan.entry, 8), f"{sig.symbol} AI entry (chart read)",
                            f"vision chart read ({model_label}); {floor_txt}",
                            derived_from, "entry")
    stop_cite = _plan_cite(round(plan.stop, 8), f"{sig.symbol} AI stop",
                           f"chart invalidation level from the vision read; {floor_txt}",
                           derived_from, "stop")
    tp1_cite = registry.register_derived(
        round(plan.tp1, 8), f"{sig.symbol} TP1 (AI plan)",
        formula="entry + 2*(entry - stop)", derived_from=[entry_cite.id, stop_cite.id],
    )
    tp2_cite = registry.register_derived(
        round(plan.tp2, 8), f"{sig.symbol} TP2 (AI plan)",
        formula="entry + 3*(entry - stop)", derived_from=[entry_cite.id, stop_cite.id],
    )
    read_cite = registry.register_direct(
        read["rationale"] or "multi-timeframe chart read", f"{sig.symbol} AI chart read",
        source_tool="llm_vision", column="rationale",
    )
    conf_cite = registry.register_direct(
        round(read["confidence"], 2), f"{sig.symbol} AI chart-read confidence",
        source_tool="llm_vision", column="confidence",
    )
    return [entry_cite.id, stop_cite.id, tp1_cite.id, tp2_cite.id,
            read_cite.id, conf_cite.id]


def _rerender_final_chart(sig: Signal, csvs: dict[str, Path], run_dir: Path,
                          bus: EventBus, disclosures: list[str],
                          charts: dict[str, dict[str, str]]) -> None:
    """Re-render one entry chart with the final AI-chosen levels so the trace
    and gallery show the plan the AI actually picked."""
    tf = next((t for t in ("15m", "5m", "30m", "1m", "1h", "1d") if t in csvs), None)
    if tf is None:
        return
    levels = {"entry": sig.entry, "stop": sig.stop, "TP1": sig.tp1, "TP2": sig.tp2}
    _render_one_chart(sig.symbol, tf, csvs[tf], run_dir, bus, disclosures,
                      charts, levels=levels, note="final AI plan")


def _ensure_trial(data_dir: Path) -> None:
    """Pre-register the AI-entry rule change once, before any of its outcomes
    exist (R7): criteria are frozen at declaration and judged on the ledger."""
    try:
        from signaldesk import trials as trials_mod

        if any(t.get("name") == _TRIAL_NAME for t in trials_mod.read_trials(data_dir)):
            return
        trials_mod.log_trial(
            data_dir, name=_TRIAL_NAME,
            hypothesis=("Entries chosen by a vision LLM reading the 1d->1m chart "
                        "ladder beat the deterministic ATR pullback rules on "
                        "expectancy."),
            change=("Phase 7.6: signal entries may be chosen by an AI multi-timeframe "
                    "chart read (model recorded per signal); stop distance stays "
                    "clamped to [max(0.75xATR(1d), 15x cost), 3xATR(1d)] and TPs "
                    "stay 2R/3R. Ledger records carry entry_mode='ai_chart_v1'."),
            judging=("Adopt if, over >= min_signals resolved non-demo ledger records "
                     "with entry_mode='ai_chart_v1', the mean R 95% CI lower bound "
                     "is > 0 (signaldesk outcomes). Kill if the break-even win rate "
                     "is unreachable at the observed average win/loss, or mean "
                     "cost_in_r exceeds 0.35."),
            min_signals=30, min_weeks=4,
        )
    except Exception:
        pass
