"""Market-scan engine powering WF-1 (crypto), WF-3 (forex/metals), WF-4
(watchlist). One linear runner parameterized by a MarketProfile; the LangGraph
state machine replaces it in M3.

Phases follow workflows.md: 0 plan → 1 news → 2 universe → 3 data →
4 sandbox TA → 5 sentiment/macro → 6 catalysts → 7 scoring/signals →
7.5 intraday entry refinement → 8 critique & synthesis.
"""
from __future__ import annotations

import math
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from signaldesk import costs as cost_model
from signaldesk import ledger as ledger_mod
from signaldesk import universe as universe_mod
from signaldesk.agent.events import EventBus, EventKind
from signaldesk.citations.registry import CitationRegistry
from signaldesk.markets import DEFAULT_ENTRY_TIMEFRAMES, DEFAULT_TIMEFRAMES, PROFILES, MarketProfile, pip_size
from signaldesk.report.render import to_json, to_markdown
from signaldesk.report.schema import (
    AvoidEntry,
    NewsClaim,
    ScanReport,
    SentimentReading,
    Signal,
    SignalSizing,
)
from signaldesk.sandbox.executor import run_script
from signaldesk.strategy import scoring
from signaldesk.strategy import sizing as sizing_mod
from signaldesk.tools import extract as extract_mod

_SANDBOX_SCRIPT = """\
import json
from pathlib import Path

import pandas as pd

from signaldesk.analysis.charts import render_symbol_chart
from signaldesk.analysis.indicators import compute_features

features = {}   # daily features (drive scoring)
ltf = {}        # lower-timeframe trend flags (confluence)
errors = {}
charts = {}
charts_dir = Path("output/charts")
charts_dir.mkdir(parents=True, exist_ok=True)

# group inputs by symbol: ohlcv_SYMBOL_TF.csv
groups = {}
for p in sorted(Path("input").glob("ohlcv_*.csv")):
    stem = p.stem[len("ohlcv_"):]
    symbol, tf = stem.rsplit("_", 1)
    groups.setdefault(symbol, {})[tf] = p

for symbol, frames in groups.items():
    try:
        daily = pd.read_csv(frames["1d"])
        features[symbol] = compute_features(daily)
        render_symbol_chart(daily, symbol, charts_dir / f"{symbol}-1d.png", title_suffix="(Daily)")
        charts[f"{symbol}-1d"] = str(charts_dir / f"{symbol}-1d.png")
        for tf, path in frames.items():
            if tf == "1d":
                continue
            df = pd.read_csv(path)
            lf = compute_features(df)
            ltf[symbol] = {
                "trend_aligned": bool(features[symbol]["above_sma20"] and lf["above_sma20"]),
                "ltf_above_sma20": bool(lf["above_sma20"]),
                "ltf": tf,
            }
            render_symbol_chart(df, symbol, charts_dir / f"{symbol}-{tf}.png",
                                title_suffix=f"({tf})")
            charts[f"{symbol}-{tf}"] = str(charts_dir / f"{symbol}-{tf}.png")
    except Exception as exc:
        errors[symbol] = str(exc)

out = pd.DataFrame(features).T if features else pd.DataFrame()
Path("output").mkdir(exist_ok=True)
out.to_csv("output/features.csv")
print(json.dumps({"symbols": len(features), "errors": errors,
                  "charts": charts, "ltf": ltf}))
"""


class MarketScanRequest(BaseModel):
    market: str = "crypto"
    universe_size: int = 12
    min_score: float = scoring.SCORE_THRESHOLD
    universe_override: list[str] | None = None  # WF-4: watchlist symbols
    watchlist_name: str | None = None
    entry_timeframes: list[str] | None = None  # None = DEFAULT_ENTRY_TIMEFRAMES; [] = off


@dataclass
class ToolSet:
    movers: object  # .run(per_side=) -> ToolResult
    quotes: object  # .run(symbols=[...]) -> ToolResult
    ohlcv: object   # .run(symbol=) -> ToolResult
    search: object  # .available() -> bool; .batch(queries) -> dict
    fear_greed: object | None = None   # crypto only
    altseason: object | None = None    # crypto only
    macro: object | None = None        # forex/metals: macro snapshot


@dataclass
class ScanRun:
    report: ScanReport
    report_md: str
    run_dir: Path
    trace_path: Path


def _f(x: float) -> float:
    return float(x) if x is not None and not pd.isna(x) else float("nan")


def run_market_scan(req: MarketScanRequest, tools: ToolSet, bus: EventBus, run_dir: Path) -> ScanRun:
    registry = CitationRegistry()
    disclosures: list[str] = []
    today = datetime.now(UTC).date().isoformat()
    market = req.market.lower()
    demo_mode = tools.ohlcv.__class__.__name__.startswith("Demo")

    # ---- Phase 0: plan ------------------------------------------------------
    profile: MarketProfile | None = PROFILES.get(market)
    if profile is None:
        raise ValueError(f"unknown market '{market}'; known: {sorted(PROFILES)}")
    label = f"watchlist '{req.watchlist_name}'" if req.universe_override else f"{market} scan"
    bus.emit("P0", EventKind.PHASE, f"plan: {label}, universe<= {req.universe_size}")

    # ---- Phase 1: market context research ----------------------------------
    context_claims: list[NewsClaim] = []
    # News depth (item: read the news): the top hit's article text is fetched
    # so claims quote the page itself; snippets remain the fallback (R4).
    # Skipped for demo scans (offline, deterministic) and time-budgeted.
    tavily_key = getattr(tools.search, "tavily_api_key", None)
    extract_deadline = time.monotonic() + 45.0

    def _read_article(url: str) -> str:
        if demo_mode or not url or time.monotonic() > extract_deadline:
            return ""
        try:
            return extract_mod.extract_article(url, tavily_api_key=tavily_key)
        except Exception:
            return ""

    if tools.search.available():
        dominant = {"crypto": "bitcoin", "forex": "US dollar", "metals": "gold"}.get(market, market)
        queries = [
            f"{market} market news today {today}",
            f"{dominant} price drivers today {today}",
            f"{market} market outlook this week {today}",
        ]
        for q, hits in tools.search.batch(queries).items():
            bus.emit("P1", EventKind.SEARCH, q, hits=len(hits))
            if hits:
                h = hits[0]
                claim = _read_article(h.url) or h.snippet or h.title
                context_claims.append(NewsClaim(claim=claim, url=h.url, published=h.published))
        if not demo_mode:
            disclosures.append(
                "News depth: context/catalyst lines quote the linked article's "
                "opening text where the page was fetchable, otherwise the "
                "search snippet is used."
            )
    else:
        disclosures.append("News context unavailable (configure a search provider); scan is TA/sentiment only.")
        bus.emit("P1", EventKind.WARN, "search provider unavailable")
    context_summary = (
        f"Market context as of {today}: " + " ".join(c.claim for c in context_claims)
        if context_claims else ""
    )

    # ---- Phase 2: universe ---------------------------------------------------
    if req.universe_override:
        universe = list(dict.fromkeys(req.universe_override))[: req.universe_size]
    else:
        universe: list[str] = list(profile.majors)
        if tools.movers is not None:
            movers = tools.movers.run(per_side=req.universe_size)
            bus.emit("P2", EventKind.TOOL, f"market_movers: {movers.summary}")
            if movers.degraded:
                disclosures.append("Movers list unavailable; scanning majors only.")
            else:
                movers_df = pd.read_csv(movers.csv_files[0])
                # Liquidity screen (item 29): floors apply only where a real
                # tape exists; demo data is synthetic, so demo scans skip it.
                if demo_mode:
                    kept_df, dropped = movers_df, []
                else:
                    kept_df, dropped = universe_mod.liquidity_screen(movers_df, market)
                if dropped:
                    vol_floor = universe_mod.FLOORS[market]["min_24h_volume_usd"]
                    cap_floor = universe_mod.FLOORS[market]["min_market_cap_usd"]
                    vol_cite = registry.register_direct(
                        vol_floor, "liquidity screen floor: minimum 24h volume (USD)",
                        source_tool="liquidity_screen", file="signaldesk/universe.py",
                        row_key=market, column="min_24h_volume_usd",
                    )
                    cap_cite = registry.register_direct(
                        cap_floor, "liquidity screen floor: minimum market cap (USD)",
                        source_tool="liquidity_screen", file="signaldesk/universe.py",
                        row_key=market, column="min_market_cap_usd",
                    )
                    reasons = "; ".join(f"{d['symbol']} ({d['reason']})" for d in dropped)
                    disclosures.append(
                        f"Liquidity screen dropped {len(dropped)} mover(s): {reasons} "
                        f"[{vol_cite.id}, {cap_cite.id}]"
                    )
                    bus.emit("P2", EventKind.WARN,
                             f"liquidity screen: {len(dropped)} dropped, "
                             f"{len(kept_df)} kept")
                picks = kept_df[kept_df["side"].isin(("gainer", "universe"))]["symbol"].tolist()
                for sym in picks:
                    if sym not in universe:
                        universe.append(sym)
        universe = universe[: req.universe_size]
    bus.emit("P2", EventKind.RESULT, f"universe: {', '.join(universe)}")

    # ---- Phase 3: quotes + OHLCV --------------------------------------------
    quotes_res = tools.quotes.run(symbols=universe)
    bus.emit("P3", EventKind.TOOL, f"quotes: {quotes_res.summary}")
    if quotes_res.degraded:
        disclosures.append("Some quotes failed to fetch; affected symbols dropped from scoring.")
    quotes_df = pd.read_csv(quotes_res.csv_files[0]) if quotes_res.csv_files else pd.DataFrame()

    price_cites: dict[str, str] = {}
    for row in quotes_df.itertuples():
        cite = registry.register_direct(
            round(float(row.price), 8),
            f"{row.symbol} close",
            source_tool="quotes",
            file=str(Path(quotes_res.csv_files[0]).name),
            row_key=str(row.symbol),
            column="price",
        )
        price_cites[row.symbol] = cite.id

    ohlcv_paths: dict[str, list[Path]] = {}
    primary_tf = DEFAULT_TIMEFRAMES[0]
    ldtf = [tf for tf in DEFAULT_TIMEFRAMES if tf != primary_tf]
    for sym in universe:
        if sym not in price_cites:
            continue
        series: list[Path] = []
        for tf in DEFAULT_TIMEFRAMES:
            try:
                res = tools.ohlcv.run(symbol=sym,
                                      interval=tf,
                                      period="60d" if tf in ldtf else "6mo")
                bus.emit("P3", EventKind.TOOL, f"ohlcv: {res.summary}")
                series.append(res.csv_files[0])
            except Exception as exc:
                if tf == primary_tf:
                    disclosures.append(f"OHLCV fetch failed for {sym}: {exc}")
                    bus.emit("P3", EventKind.WARN, f"ohlcv failed for {sym}")
                else:
                    # intraday missing is non-fatal; the report discloses it (R4)
                    disclosures.append(f"{sym}: no {tf} bars (multi-timeframe partially degraded)")
        daily = [p for p in series if p.stem.endswith("_1d")] or series[:1]
        if daily:
            ohlcv_paths[sym] = [daily[0]] + [p for p in series if p != daily[0]]
    if not ohlcv_paths:
        raise RuntimeError("no OHLCV data for any symbol — cannot compute signals")

    # ---- Phase 4: sandbox TA --------------------------------------------------
    sandbox_dir = run_dir / "sandbox"
    input_dir = sandbox_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    for paths in ohlcv_paths.values():
        for path in paths:
            shutil.copy(path, input_dir / path.name)
    proc = run_script(_SANDBOX_SCRIPT, sandbox_dir, timeout=180)
    bus.emit("P4", EventKind.SANDBOX, f"technical computation: {proc.stdout.strip()}")
    features_df = pd.read_csv(sandbox_dir / "output" / "features.csv", index_col=0)

    # stream one event per rendered chart (the "visual analysis" UI surface)
    charts: dict[str, str] = {}
    ltf_flags: dict[str, dict] = {}
    import json as _json2

    try:
        meta = _json2.loads(proc.stdout.strip())
        ltf_flags = meta.get("ltf") or {}
        for key, chart_path in (meta.get("charts") or {}).items():
            rel = str(Path("sandbox") / chart_path).replace("\\", "/")
            # key format: "SYMBOL-TF" e.g. BTCUSD-1d, BTCUSD-1h
            symbol, tf = key.rsplit("-", 1)
            charts.setdefault(symbol, {})[tf] = rel
            bus.emit("P4", EventKind.CHART, f"chart ready: {symbol} ({tf})", path=rel, symbol=symbol, tf=tf)
    except Exception:
        pass

    derived: dict[str, dict[str, str]] = {}
    for sym, row in features_df.iterrows():
        base = [price_cites[sym]]
        derived[sym] = {
            "rsi": registry.register_derived(
                round(_f(row["rsi14"]), 2), f"{sym} RSI(14)",
                formula="RSI(close,14) Wilder", derived_from=base,
            ).id,
            "macd_hist": registry.register_derived(
                round(_f(row["macd_hist"]), 8), f"{sym} MACD histogram",
                formula="EMA12-EMA26 minus signal(9)", derived_from=base,
            ).id,
            "atr": registry.register_derived(
                round(_f(row["atr14"]), 8), f"{sym} ATR(14)",
                formula="ATR(H,L,C,14) Wilder", derived_from=base,
            ).id,
        }

    # ---- Phase 5: sentiment (crypto) / macro (forex, metals) ------------------
    sentiment: list[SentimentReading] = []
    fng_value: float | None = None
    macro = scoring.MacroInputs()
    if profile.preset == "crypto":
        for tool in (t for t in (tools.fear_greed, tools.altseason) if t is not None):
            res = tool.run()
            bus.emit("P5", EventKind.TOOL, f"{tool.name}: {res.summary}")
            if res.degraded or not res.csv_files:
                disclosures.append(f"{tool.name} unavailable; sentiment partially degraded.")
                continue
            row = pd.read_csv(res.csv_files[0]).iloc[0]
            cite = registry.register_direct(
                _f(row["value"]), str(row["index"]),
                source_tool=tool.name, file=Path(res.csv_files[0]).name,
                row_key=str(row["index"]), column="value",
            )
            sentiment.append(SentimentReading(
                index=str(row["index"]), value=_f(row["value"]), regime=str(row["regime"]),
                citations=[cite.id],
            ))
            if row["index"] == "fear_greed":
                fng_value = _f(row["value"])
    else:
        if tools.macro is None:
            disclosures.append("Macro snapshot unavailable; forex macro component scored 0.")
        else:
            res = tools.macro.run()
            bus.emit("P5", EventKind.TOOL, f"macro_snapshot: {res.summary}")
            if res.degraded or not res.csv_files:
                disclosures.append("Macro snapshot unavailable; forex macro component scored 0.")
            else:
                row = pd.read_csv(res.csv_files[0]).iloc[0]
                macro = scoring.MacroInputs(
                    dxy_chg_30d_pct=_f(row["dxy_chg_30d_pct"]),
                    us10y_chg_30d_bp=_f(row["us10y_chg_30d_bp"]),
                )
                dxy_cite = registry.register_direct(
                    round(_f(row["dxy"]), 2), "DXY 30d change %",
                    source_tool="macro_snapshot", file=Path(res.csv_files[0]).name,
                    row_key="macro", column="dxy_chg_30d_pct",
                )
                rate_cite = registry.register_direct(
                    round(_f(row["us10y"]), 3), "US10Y 30d change (bp)",
                    source_tool="macro_snapshot", file=Path(res.csv_files[0]).name,
                    row_key="macro", column="us10y_chg_30d_bp",
                )
                sentiment.append(SentimentReading(
                    index="dxy_30d", value=_f(row["dxy_chg_30d_pct"]),
                    regime="USD firms" if _f(row["dxy_chg_30d_pct"]) > 0 else "USD softens",
                    citations=[dxy_cite.id],
                ))
                sentiment.append(SentimentReading(
                    index="us10y_30d", value=_f(row["us10y_chg_30d_bp"]),
                    regime="yields rising" if _f(row["us10y_chg_30d_bp"]) > 0 else "yields falling",
                    citations=[rate_cite.id],
                ))
                if not math.isfinite(macro.dxy_chg_30d_pct or math.nan):
                    disclosures.append("DXY 30d change missing; macro component partial.")

    # ---- Phase 7 (scoring) + Phase 6 (catalysts) ----------------------------
    breakdowns: dict[str, scoring.ScoreBreakdown] = {}
    feats: dict[str, scoring.SymbolFeatures] = {}
    for sym, row in features_df.iterrows():
        feat = scoring.SymbolFeatures(
            close=_f(row["close"]), rsi14=_f(row["rsi14"]), atr14=_f(row["atr14"]),
            swing_low_20=_f(row["swing_low_20"]),
            above_sma20=bool(row["above_sma20"]), above_sma50=bool(row["above_sma50"]),
            ema9_above_ema21=bool(row["ema9_above_ema21"]),
            macd_hist=_f(row["macd_hist"]), macd_hist_prev=_f(row["macd_hist_prev"]),
            volume=_f(row["volume"]), volume_avg30=_f(row["volume_avg30"]),
        )
        feats[sym] = feat
        if profile.preset == "fx":
            breakdowns[sym] = scoring.score_fx_symbol(feat, macro, scoring.usd_leg(sym))
        else:
            breakdowns[sym] = scoring.score_symbol(feat, fng_value)

    catalysts: dict[str, list[str]] = {s: [] for s in breakdowns}
    if tools.search.available():
        top5 = sorted(breakdowns, key=lambda s: breakdowns[s].total, reverse=True)[:5]
        month = today[:7]
        for sym in top5:
            base = sym[:-3] if (market == "crypto" and sym.endswith("USD")) else sym
            queries = [f"{base} price surge news catalyst", f"{base} news {month}"]
            for q, hits in tools.search.batch(queries, max_results=2).items():
                bus.emit("P6", EventKind.SEARCH, q, hits=len(hits))
                for h in hits[:1]:
                    extracted = _read_article(h.url)
                    claim = extracted or (h.snippet or h.title)[:240]
                    cite = registry.register_direct(
                        claim, f"{sym} catalyst",
                        source_tool="web_search",
                        column="extracted_text" if extracted else "snippet",
                        row_key=h.url[:120],
                    )
                    catalysts[sym].append(f"{claim} [{cite.id}]")

    # ---- Phase 7 cont.: signals ----------------------------------------------
    signals: list[Signal] = []
    avoid: list[AvoidEntry] = []
    cost_cite_ids: dict[str, str] = {}
    for sym, score in sorted(breakdowns.items(), key=lambda kv: kv[1].total, reverse=True):
        f = feats[sym]
        cites = [derived[sym]["rsi"], derived[sym]["macd_hist"], derived[sym]["atr"]]
        if sym in price_cites:
            cites.append(price_cites[sym])
        # assumed round-trip cost is a cited assumption, not market data
        cost_pct = cost_model.round_trip_cost_pct(market, sym)
        cost_cite = registry.register_direct(
            cost_pct, f"{sym} assumed round-trip cost %",
            source_tool="cost_model", file="signaldesk/costs.py",
            row_key=sym, column="round_trip_cost_pct",
        )
        cost_cite_ids[sym] = cost_cite.id
        if score.overextended:
            avoid.append(AvoidEntry(
                symbol=sym,
                reason=f"RSI {f.rsi14:.1f} >= 75 — overextended, long setup disallowed (R3)",
                citations=cites,
            ))
            continue
        if score.hold_capped:
            avoid.append(AvoidEntry(
                symbol=sym,
                reason=f"Fear & Greed {fng_value:.0f} — extreme greed caps all signals at HOLD",
                citations=cites,
            ))
            continue
        if score.total < req.min_score:
            continue
        cites.append(cost_cite.id)
        plan = scoring.build_trade_plan(f.close, f.atr14, f.swing_low_20, cost_pct=cost_pct)
        risk_pct = (plan.entry - plan.stop) / plan.entry * 100.0 if plan.entry else 0.0
        cost_r = cost_model.cost_in_r(cost_pct, risk_pct)
        confluence = list(score.reasons)
        if plan.floored:
            confluence.append(f"stop floored: {scoring.MIN_RISK_ATR_MULT:g}xATR(1d) / "
                              f"{scoring.MIN_RISK_COST_MULT:g}x cost keeps cost at {cost_r:.2f}R")
        ltf = ltf_flags.get(sym)
        if ltf and ltf.get("ltf_above_sma20") is not None:
            tf = ltf.get("ltf", DEFAULT_TIMEFRAMES[1] if len(DEFAULT_TIMEFRAMES) > 1 else "1h")
            if ltf["ltf_above_sma20"]:
                ltf_cite = registry.register_derived(
                    "aligned", f"{sym} {tf} trend alignment",
                    formula=f"SMA20({tf}) vs close({tf})", derived_from=[price_cites[sym]],
                )
                confluence.append(f"{tf} trend aligned (above SMA20 on both timeframes)")
                cites.append(ltf_cite.id)
            else:
                confluence.append(f"{tf} trend divergent — LTF caution")
                disclosures.append(f"{sym}: {tf} trend disagrees with daily")
        pip = pip_size(sym)
        if not math.isnan(pip) and profile.session_note:
            confluence.append(f"{profile.session_note} Risk {abs(plan.entry - plan.stop) / pip:.0f} pips")
        elif profile.session_note:
            confluence.append(profile.session_note)
        signals.append(Signal(
            symbol=sym, score=score.total,
            entry=round(plan.entry, 8), stop=round(plan.stop, 8),
            tp1=round(plan.tp1, 8), tp2=round(plan.tp2, 8),
            confluence=confluence, catalysts=catalysts.get(sym, []), citations=cites,
            cost_pct=cost_pct, cost_in_r=round(cost_r, 4),
        ))
    signals = signals[:5]

    # ---- Phase 7.5: intraday entry refinement --------------------------------
    entry_tfs = DEFAULT_ENTRY_TIMEFRAMES if req.entry_timeframes is None else tuple(req.entry_timeframes)
    if entry_tfs and signals:
        from signaldesk.workflows.entry_refine import refine_signal_entries

        refine_signal_entries(
            signals, list(entry_tfs), tools.ohlcv, run_dir, bus, registry, disclosures, charts,
            price_cite_ids={s.symbol: price_cites[s.symbol] for s in signals if s.symbol in price_cites},
            atr_cite_ids={s.symbol: derived[s.symbol]["atr"] for s in signals if s.symbol in derived},
            atr_daily={s.symbol: feats[s.symbol].atr14 for s in signals if s.symbol in feats},
            cost_pcts={s.symbol: s.cost_pct for s in signals},
            cost_cite_ids=cost_cite_ids,
        )

    if signals:
        assumptions = ", ".join(f"{s.symbol} {s.cost_pct:g}%" for s in signals)
        disclosures.append(
            f"Cost model: assumed round-trip cost {assumptions} of notional "
            "(fees + spread + slippage). Stops are floored so the cost stays a "
            "small fraction of the risk unit; cost-in-R is shown per signal."
        )

    # ---- Position sizing (item 30): recommended account risk, advice only ----
    risk_pcts = {}
    for s in signals:
        risk = abs(s.entry - s.stop)
        if s.entry and risk > 0:
            risk_pcts[s.symbol] = risk / s.entry * 100.0
    sizes = sizing_mod.size_signals(risk_pcts)
    for s in signals:
        sz = sizes.get(s.symbol)
        if sz is not None:
            s.sizing = SignalSizing(
                risk_pct_account=sz.risk_pct_account,
                notional_pct_account=sz.notional_pct_account,
                weight=sz.weight, capped=sz.capped)
    if sizes:
        disclosures.append(
            "Sizing model (advice only, never executed): inverse-volatility "
            f"account risk {sizing_mod.MIN_RISK_PCT:g}-{sizing_mod.MAX_RISK_PCT:g}% "
            f"per trade, cluster-capped at {sizing_mod.CLUSTER_CAP_RISK_PCT:g}% per "
            "market — correlated positions are fewer independent bets, not ten."
        )
        if any(sz.capped for sz in sizes.values()):
            total = sum(sz.risk_pct_account for sz in sizes.values())
            disclosures.append(
                f"Cluster cap applied: recommended risks summed past "
                f"{sizing_mod.CLUSTER_CAP_RISK_PCT:g}% of account and were scaled "
                f"down to {total:.2f}% total."
            )

    # ---- Phase 8: critique & synthesis ----------------------------------------
    preset_name = profile.preset if profile.preset == "crypto" else scoring.FX_PRESET_NAME
    if profile.preset == "crypto":
        preset_name = scoring.PRESET_NAME
    report = ScanReport(
        market=market,
        as_of=datetime.now(UTC).isoformat(timespec="seconds"),
        scoring_preset=preset_name,
        universe=universe,
        context_summary=context_summary,
        context_claims=context_claims,
        sentiment=sentiment,
        signals=signals,
        avoid=avoid,
        disclosures=disclosures,
        citations=registry.all(),
        charts=charts,
    )
    coverage = report.citation_coverage
    if coverage < 1.0:
        disclosures.append(f"Critic: citation coverage {coverage:.0%} below 100%")
    if any(not math.isfinite(s.entry) for s in signals):
        disclosures.append("Critic: non-finite price detected in a signal")
    bus.emit(
        "P8", EventKind.RESULT,
        f"report: {len(signals)} signals, {len(avoid)} avoided, "
        f"{len(registry)} citations, coverage {coverage:.0%}",
    )

    # ---- Ledger: freeze every emitted signal for later outcome scoring --------
    bars_last = {sym: str(row.get("as_of") or "") for sym, row in features_df.iterrows()}
    data_hashes = {sym: ledger_mod.sha256_file(paths[0]) for sym, paths in ohlcv_paths.items()}

    # Signal-time context (trial T1 enrichment): the regime/chase numbers a
    # later conditioning analysis needs, frozen at decision time. Missing
    # pieces are simply omitted — never guessed.
    contexts: dict[str, dict[str, float]] = {}
    quotes_by_sym = {str(row.symbol): row for row in quotes_df.itertuples()}
    btc_mom = None
    if market == "crypto" and "BTCUSD" in ohlcv_paths:
        try:
            btc_daily = pd.read_csv(ohlcv_paths["BTCUSD"][0])
            if len(btc_daily) > 20:
                closes = btc_daily["close"].astype(float)
                btc_mom = float(closes.iloc[-1] / closes.iloc[-21] - 1) * 100.0
        except Exception:
            btc_mom = None
    for sym, row in features_df.iterrows():
        ctx: dict[str, float] = {}
        q = quotes_by_sym.get(sym)
        if q is not None and getattr(q, "change_24h_pct", None) is not None:
            ctx["change_24h_pct"] = float(q.change_24h_pct)
        vol, vol_avg = _f(row.get("volume")), _f(row.get("volume_avg30"))
        if math.isfinite(vol_avg) and vol_avg > 0 and math.isfinite(vol):
            ctx["volume_ratio"] = vol / vol_avg
        for k in ("rsi14", "sma20", "sma50", "atr14"):
            v = _f(row.get(k))
            if math.isfinite(v):
                ctx[k] = v
        close_v, sma20_v, sma50_v = _f(row.get("close")), _f(row.get("sma20")), _f(row.get("sma50"))
        if math.isfinite(close_v) and math.isfinite(sma20_v) and sma20_v:
            ctx["sma20_dist_pct"] = (close_v / sma20_v - 1.0) * 100.0
        if math.isfinite(close_v) and math.isfinite(sma50_v) and sma50_v:
            ctx["sma50_dist_pct"] = (close_v / sma50_v - 1.0) * 100.0
        if btc_mom is not None:
            ctx["btc_mom_20d_pct"] = btc_mom
        contexts[sym] = ctx

    ledger_records = ledger_mod.record_report(
        report, run_dir.name, market=market,
        data_hashes=data_hashes, bars_last_dates=bars_last,
        cost_pcts={s.symbol: s.cost_pct for s in signals},
        demo=demo_mode, contexts=contexts,
    )
    ledger_file = ledger_mod.default_ledger_path(run_dir)
    written = ledger_mod.append_records(ledger_file, ledger_records, dedupe=True)
    if written:
        bus.emit("P8", EventKind.RESULT,
                 f"ledger: {written} signal(s) recorded -> {ledger_file}")

    report_md = to_markdown(report)
    (run_dir / "report.md").write_text(report_md, encoding="utf-8")
    (run_dir / "report.json").write_text(to_json(report), encoding="utf-8")
    trace_path = run_dir / "trace.jsonl"
    trace_path.write_text(bus.to_jsonl(), encoding="utf-8")
    return ScanRun(report=report, report_md=report_md, run_dir=run_dir, trace_path=trace_path)
