"""Market-scan engine powering WF-1 (crypto), WF-3 (forex/metals), WF-4
(watchlist). One linear runner parameterized by a MarketProfile; the LangGraph
state machine replaces it in M3.

Phases follow workflows.md: 0 plan → 1 news → 2 universe → 3 data →
4 sandbox TA → 5 sentiment/macro → 6 catalysts → 7 scoring/signals →
7.5 intraday entry refinement → 7.6 AI chart read (vision entry
selection) → 8 critique & synthesis.
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
from signaldesk import learn as learn_mod
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


def _g(v: float) -> str:
    """Compact finite-float formatter for AI briefs ("n/a" for NaN/None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f:.6g}" if math.isfinite(f) else "n/a"


def _ai_gate_warnings(sym: str, direction: str, feat_row, fng_value: float | None,
                      btc_below: bool, btc_above: bool, registry, price_cites):
    """Policy-gate violations for an AI pick as (text, cite id) pairs.

    Advisory on the AI path (operator choice, disclosed): a violation is shown
    as a warning on the signal, never a silent pass and never a veto. The
    fallback path keeps the hard vetoes."""
    out = []
    verdict = (scoring.regime_gate_short(feat_row) if direction == "SHORT"
               else scoring.regime_gate(feat_row))
    if verdict.reason:
        wcite = None
        if sym in price_cites and verdict.value is not None:
            wcite = registry.register_derived(
                round(verdict.value, 4), f"{sym} policy gate: {verdict.column}",
                formula=verdict.formula, derived_from=[price_cites[sym]]).id
        out.append((verdict.reason, wcite))
    rsi = _f(feat_row.get("rsi14"))
    if direction != "SHORT" and math.isfinite(rsi) and rsi >= scoring.RSI_OVEREXTENDED:
        out.append((f"RSI {rsi:.1f} >= {scoring.RSI_OVEREXTENDED:.0f} overextension guard", None))
    if direction == "SHORT" and math.isfinite(rsi) and rsi <= scoring.SHORT_RSI_OVEREXTENDED:
        out.append((f"RSI {rsi:.1f} <= {scoring.SHORT_RSI_OVEREXTENDED:.0f} falling-knife guard", None))
    if fng_value is not None and direction != "SHORT" and fng_value >= scoring.FNG_EXTREME_GREED:
        out.append((f"Fear & Greed {fng_value:.0f} — extreme greed cap", None))
    if fng_value is not None and direction == "SHORT" and fng_value <= scoring.FNG_EXTREME_FEAR:
        out.append((f"Fear & Greed {fng_value:.0f} — extreme fear cap", None))
    if direction != "SHORT" and btc_below:
        out.append(("market regime: BTC below its 200d SMA (item 32 long gate)", None))
    if direction == "SHORT" and btc_above:
        out.append(("market regime: BTC above its 200d SMA (item 32 short mirror)", None))
    return out


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

    # ---- Phase 6.5: AI signal generation (vision LLM decides) ----------------
    # When enabled (Settings, non-demo, LLM creds present), the vision LLM —
    # not the deterministic preset — decides which symbols become signals: one
    # call per symbol over its 1d+1h charts and a data brief. The deterministic
    # Phase 7 engine below remains as the whole-scan fallback (demo, no key,
    # setting off, no usable AI reads) and every fallback is disclosed (R4).
    feats: dict[str, scoring.SymbolFeatures] = {}
    for sym, row in features_df.iterrows():
        feats[sym] = scoring.SymbolFeatures(
            close=_f(row["close"]), rsi14=_f(row["rsi14"]), atr14=_f(row["atr14"]),
            swing_low_20=_f(row["swing_low_20"]),
            above_sma20=bool(row["above_sma20"]), above_sma50=bool(row["above_sma50"]),
            ema9_above_ema21=bool(row["ema9_above_ema21"]),
            macd_hist=_f(row["macd_hist"]), macd_hist_prev=_f(row["macd_hist_prev"]),
            volume=_f(row["volume"]), volume_avg30=_f(row["volume_avg30"]),
            sma20=_f(row.get("sma20")), sma50=_f(row.get("sma50")),
            ema9=_f(row.get("ema9")), ema21=_f(row.get("ema21")),
        )

    # BTC book-regime flags (item 32, trial T2): hard vetoes on the fallback
    # path; advisory warnings on the AI path (operator choice, disclosed).
    market_regime_reason = ""        # non-empty: long book stands down
    market_regime_reason_short = ""  # non-empty: short book stands down
    btc_gate_cite = None
    btc_below = btc_above = False
    if market == "crypto" and "BTCUSD" in features_df.index:
        btc_feat = features_df.loc["BTCUSD"]
        btc_below = scoring.market_regime_below_trend(btc_feat)
        btc_above = scoring.market_regime_above_trend(btc_feat)
        if btc_below or btc_above:
            btc_close = _f(btc_feat.get("close"))
            btc_sma200 = _f(btc_feat.get("sma200"))
            if "BTCUSD" in price_cites and btc_sma200:
                btc_gate_cite = registry.register_derived(
                    round(btc_close / btc_sma200, 4), "BTC 200d regime ratio",
                    formula="close / SMA200 (< 1 caps longs, > 1 caps shorts)",
                    derived_from=[price_cites["BTCUSD"]]).id
        if btc_below:
            market_regime_reason = ("market regime: BTC below its 200d SMA — the "
                                    "long-only engine stands down (item 32)")
        if btc_above:
            market_regime_reason_short = ("market regime: BTC above its 200d SMA — "
                                          "the short book stands down (item 32 mirror)")

    use_ai_engine = False
    generator_model = ""
    picks: dict[str, dict] = {}
    if not demo_mode:
        try:
            from signaldesk import userconfig as _uc

            gen_enabled = _uc.Settings(run_dir.parent.parent).get("ai_signal_generation")
        except Exception:
            gen_enabled = True
        if gen_enabled:
            from signaldesk.agent import vision as vision_mod

            creds = vision_mod.resolve_creds()
            if creds is None:
                disclosures.append(
                    "AI signal generation unavailable: no LLM key configured — "
                    "deterministic preset used (R4).")
            else:
                from signaldesk.workflows import ai_generate as ai_gen

                gen_provider, gen_key, gen_model = creds
                generator_model = vision_mod.model_id(gen_provider, gen_model)
                altseason_value = next((s.value for s in sentiment
                                        if s.index == "altcoin_season"), None)
                btc_note = ""
                if market == "crypto" and "BTCUSD" in features_df.index:
                    _btc_close = _f(features_df.loc["BTCUSD"].get("close"))
                    _btc_ma = _f(features_df.loc["BTCUSD"].get("sma200"))
                    _side = "below" if btc_below else ("above" if btc_above else "near")
                    btc_note = (f"market context: BTC {_side} its 200d SMA "
                                f"({_g(_btc_close)} vs {_g(_btc_ma)})")
                changes_24h = {str(r.symbol): (float(r.change_24h_pct)
                                               if getattr(r, "change_24h_pct", None) is not None else None)
                               for r in quotes_df.itertuples()}
                bus.emit("P6.5", EventKind.PHASE,
                         f"AI signal generation: {len(features_df.index)} symbol(s) "
                         f"(model {generator_model})")
                picks = ai_gen.generate_picks(
                    features_df, charts, run_dir, bus, disclosures,
                    provider=gen_provider, key=gen_key, model=gen_model,
                    model_label=generator_model, fng_value=fng_value,
                    altseason_value=altseason_value, btc_note=btc_note,
                    context_summary=context_summary, changes_24h=changes_24h)
                use_ai_engine = bool(picks)
                if not use_ai_engine:
                    disclosures.append(
                        "AI signal generation returned no usable reads — "
                        "deterministic preset used (R4).")

    # ---- Phase 7: signals (AI-generated, or deterministic fallback) ----------
    signals: list[Signal] = []
    avoid: list[AvoidEntry] = []
    cost_cite_ids: dict[str, str] = {}
    low_history_syms: set[str] = set()

    if use_ai_engine:
        # The vision model's picks ARE the signals (Phase 6.5): direction and
        # conviction come from the charts + data brief; the stop starts at the
        # AI's invalidation level, clamped to the R6 risk floor. Policy gates
        # are advisory warnings here (operator choice) — hard vetoes only on
        # the fallback path below.
        from signaldesk.workflows.ai_generate import _ensure_generation_trial

        for sym, pick in sorted(picks.items(), key=lambda kv: kv[1]["score"], reverse=True):
            f = feats[sym]
            feat_row = features_df.loc[sym]
            decision_cite = registry.register_direct(
                pick["rationale"] or pick["direction"], f"{sym} AI signal decision",
                source_tool="llm_vision", column="decision").id
            cost_pct = cost_model.round_trip_cost_pct(market, sym)
            if pick["direction"] == "NONE" or pick["score"] < req.min_score:
                avoid.append(AvoidEntry(
                    symbol=sym,
                    reason=(f"AI passed: {pick['direction']} score "
                            f"{pick['score']:.0f}/100 (threshold {req.min_score:.0f}) "
                            f"— {pick['rationale']}"),
                    citations=[decision_cite]))
                continue
            cites = [derived[sym]["rsi"], derived[sym]["macd_hist"], derived[sym]["atr"]]
            if sym in price_cites:
                cites.append(price_cites[sym])
            cost_cite = registry.register_direct(
                cost_pct, f"{sym} assumed round-trip cost %",
                source_tool="cost_model", file="signaldesk/costs.py",
                row_key=sym, column="round_trip_cost_pct",
            )
            cost_cite_ids[sym] = cost_cite.id
            cites.append(cost_cite.id)
            plan = scoring.ai_draft_plan(f.close, f.atr14, direction=pick["direction"],
                                         invalidation=pick["invalidation"],
                                         cost_pct=cost_pct)
            risk_pct = abs(plan.entry - plan.stop) / plan.entry * 100.0 if plan.entry else 0.0
            cost_r = cost_model.cost_in_r(cost_pct, risk_pct)
            confluence = [f"AI decision: {pick['direction']} score "
                          f"{pick['score']:.0f}/100 — {pick['rationale']}"]
            for wtxt, wcite in _ai_gate_warnings(sym, pick["direction"], feat_row,
                                                 fng_value, btc_below, btc_above,
                                                 registry, price_cites):
                confluence.append(f"⚠ policy gate: {wtxt} — AI proceeded")
                if wcite:
                    cites.append(wcite)
            if plan.floored:
                confluence.append(f"stop floored: {scoring.MIN_RISK_ATR_MULT:g}xATR(1d) / "
                                  f"{scoring.MIN_RISK_COST_MULT:g}x cost keeps cost at {cost_r:.2f}R")
            if profile.session_note:
                confluence.append(profile.session_note)
            signals.append(Signal(
                symbol=sym, direction=pick["direction"], score=pick["score"],
                entry=round(plan.entry, 8), stop=round(plan.stop, 8),
                tp1=round(plan.tp1, 8), tp2=round(plan.tp2, 8),
                confluence=confluence, catalysts=[], citations=cites,
                cost_pct=cost_pct, cost_in_r=round(cost_r, 4),
                ai_generated=True,
            ))
        if signals:
            disclosures.append(
                "AI-generated signals (Phase 6.5): selection, direction and score "
                f"come from the vision model ({generator_model}) reading each "
                "symbol's daily+1h charts and data brief; policy gates are shown "
                "as warnings, not vetoes (operator choice, pre-registered trial "
                "ai-signal-generation-v1); draft stops stay floored at "
                "max(0.75xATR(1d), 15x cost) and TPs at 2R/3R.")
            _ensure_generation_trial(run_dir.parent.parent)

    if not use_ai_engine:
        # ---- Deterministic engine (fallback + demo/no-key path) --------------
        if market == "crypto" and btc_below:
            bus.emit("P7", EventKind.WARN, "regime gate: BTC below 200d SMA — longs capped")
        if market == "crypto" and btc_above:
            bus.emit("P7", EventKind.WARN, "regime gate: BTC above 200d SMA — shorts capped")
        breakdowns: dict[str, scoring.ScoreBreakdown] = {}        # long side
        breakdowns_short: dict[str, scoring.ScoreBreakdown] = {}  # short side
        for sym, feat in feats.items():
            if profile.preset == "fx":
                breakdowns[sym] = scoring.score_fx_symbol(feat, macro, scoring.usd_leg(sym))
                breakdowns_short[sym] = scoring.score_fx_symbol_short(feat, macro, scoring.usd_leg(sym))
            else:
                breakdowns[sym] = scoring.score_symbol(feat, fng_value)
                breakdowns_short[sym] = scoring.score_short_symbol(feat, fng_value)

        def _best_total(sym: str) -> float:
            return max(breakdowns[sym].total, breakdowns_short[sym].total)

        for sym in sorted(breakdowns, key=_best_total, reverse=True):
            f = feats[sym]
            feat_row = features_df.loc[sym]
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

            # Per-side gate pass, each mirroring the other's order: overextension,
            # sentiment extreme, book regime, then the per-symbol regime gates.
            blocked: dict[str, tuple[str, list[str]]] = {}  # side -> (reason, gate cites)

            bd = breakdowns[sym]
            if bd.overextended:
                blocked["LONG"] = (f"RSI {f.rsi14:.1f} >= 75 — overextended, long setup disallowed (R3)", [])
            elif bd.hold_capped:
                blocked["LONG"] = (f"Fear & Greed {fng_value:.0f} — extreme greed caps all signals at HOLD", [])
            elif market_regime_reason:
                blocked["LONG"] = (market_regime_reason
                                   + (f" [{btc_gate_cite}]" if btc_gate_cite else ""),
                                   [btc_gate_cite] if btc_gate_cite else [])
            else:
                verdict = scoring.regime_gate(feat_row)
                if not verdict.reason:
                    sma200_v = _f(feat_row.get("sma200"))
                    if not math.isfinite(sma200_v):
                        low_history_syms.add(sym)   # <200d history: trend gate unevaluated (R4)
                if verdict.reason:
                    gate_cite = None
                    if sym in price_cites:
                        gate_cite = registry.register_derived(
                            round(verdict.value, 4) if verdict.value is not None else 0.0,
                            f"{sym} regime gate: {verdict.column}",
                            formula=verdict.formula, derived_from=[price_cites[sym]],
                        ).id
                    blocked["LONG"] = (f"{verdict.reason} [{gate_cite}]" if gate_cite else verdict.reason,
                                       [gate_cite] if gate_cite else [])

            bd_s = breakdowns_short[sym]
            if bd_s.overextended:
                blocked["SHORT"] = (f"RSI {f.rsi14:.1f} <= 25 — falling knife, short setup disallowed (R3 mirror)", [])
            elif bd_s.hold_capped:
                blocked["SHORT"] = (f"Fear & Greed {fng_value:.0f} — extreme fear caps short signals at HOLD", [])
            elif market_regime_reason_short:
                blocked["SHORT"] = (market_regime_reason_short
                                    + (f" [{btc_gate_cite}]" if btc_gate_cite else ""),
                                    [btc_gate_cite] if btc_gate_cite else [])
            else:
                verdict_s = scoring.regime_gate_short(feat_row)
                if not verdict_s.reason:
                    sma200_v = _f(feat_row.get("sma200"))
                    if not math.isfinite(sma200_v):
                        low_history_syms.add(sym)
                if verdict_s.reason:
                    gate_cite = None
                    if sym in price_cites:
                        gate_cite = registry.register_derived(
                            round(verdict_s.value, 4) if verdict_s.value is not None else 0.0,
                            f"{sym} regime gate (short): {verdict_s.column}",
                            formula=verdict_s.formula, derived_from=[price_cites[sym]],
                        ).id
                    blocked["SHORT"] = (f"{verdict_s.reason} [{gate_cite}]" if gate_cite else verdict_s.reason,
                                        [gate_cite] if gate_cite else [])

            eligible = []
            for side in ("LONG", "SHORT"):
                total = breakdowns[sym].total if side == "LONG" else breakdowns_short[sym].total
                if side not in blocked and total >= req.min_score:
                    eligible.append((side, total))
            if not eligible:
                if blocked:
                    reason_txt = " | ".join(f"{side} refused: {reason}"
                                            for side, (reason, _cs) in blocked.items())
                    avoid_cites = list(cites) + [c for (_r, cs) in blocked.values() for c in cs]
                    avoid.append(AvoidEntry(symbol=sym, reason=reason_txt, citations=avoid_cites))
                continue

            # LONG wins an exact tie (listed first): the conservative default.
            direction = max(eligible, key=lambda kv: kv[1])[0]
            bd = breakdowns[sym] if direction == "LONG" else breakdowns_short[sym]
            cites.append(cost_cite.id)
            if direction == "LONG":
                plan = scoring.build_trade_plan(f.close, f.atr14, f.swing_low_20, cost_pct=cost_pct)
            else:
                plan = scoring.build_short_trade_plan(f.close, f.atr14,
                                                      _f(feat_row.get("swing_high_20")),
                                                      cost_pct=cost_pct)
            risk_pct = abs(plan.entry - plan.stop) / plan.entry * 100.0 if plan.entry else 0.0
            cost_r = cost_model.cost_in_r(cost_pct, risk_pct)
            confluence = list(bd.reasons)
            if plan.floored:
                confluence.append(f"stop floored: {scoring.MIN_RISK_ATR_MULT:g}xATR(1d) / "
                                  f"{scoring.MIN_RISK_COST_MULT:g}x cost keeps cost at {cost_r:.2f}R")
            ltf = ltf_flags.get(sym)
            if ltf and ltf.get("ltf_above_sma20") is not None:
                tf = ltf.get("ltf", DEFAULT_TIMEFRAMES[1] if len(DEFAULT_TIMEFRAMES) > 1 else "1h")
                if direction == "LONG":
                    aligned = bool(ltf["ltf_above_sma20"])
                else:
                    aligned = not bool(ltf["ltf_above_sma20"]) and not f.above_sma20
                if aligned:
                    ltf_cite = registry.register_derived(
                        "aligned", f"{sym} {tf} trend alignment",
                        formula=f"SMA20({tf}) vs close({tf})", derived_from=[price_cites[sym]],
                    )
                    side_txt = "above" if direction == "LONG" else "below"
                    confluence.append(f"{tf} trend aligned ({side_txt} SMA20 on both timeframes)")
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
                symbol=sym, direction=direction, score=bd.total,
                entry=round(plan.entry, 8), stop=round(plan.stop, 8),
                tp1=round(plan.tp1, 8), tp2=round(plan.tp2, 8),
                confluence=confluence, catalysts=[], citations=cites,
                cost_pct=cost_pct, cost_in_r=round(cost_r, 4),
            ))
    signals = signals[:5]

    # ---- Phase 6 (catalysts): news research for the final signals ------------
    catalysts: dict[str, list[str]] = {}
    if tools.search.available() and signals:
        month = today[:7]
        for s in signals:
            sym = s.symbol
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
                    catalysts.setdefault(sym, []).append(f"{claim} [{cite.id}]")
        for s in signals:
            s.catalysts = catalysts.get(s.symbol, [])
    low_history = len(low_history_syms)

    # ---- Phase 7.5: intraday entry refinement --------------------------------
    # Deterministic LTF rules are written for the long side, so they refine
    # LONG signals only — SHORT signals keep their daily plan unless the
    # Phase 7.6 AI chart read chooses their entry (disclosed, never silent, R4).
    entry_tfs = DEFAULT_ENTRY_TIMEFRAMES if req.entry_timeframes is None else tuple(req.entry_timeframes)
    long_signals = [s for s in signals if s.direction != "SHORT"]
    short_signals = [s for s in signals if s.direction == "SHORT"]
    ltf_csvs: dict[str, dict[str, Path]] = {}
    if entry_tfs and long_signals:
        from signaldesk.workflows.entry_refine import refine_signal_entries

        refine_signal_entries(
            long_signals, list(entry_tfs), tools.ohlcv, run_dir, bus, registry, disclosures, charts,
            price_cite_ids={s.symbol: price_cites[s.symbol] for s in long_signals if s.symbol in price_cites},
            atr_cite_ids={s.symbol: derived[s.symbol]["atr"] for s in long_signals if s.symbol in derived},
            atr_daily={s.symbol: feats[s.symbol].atr14 for s in long_signals if s.symbol in feats},
            cost_pcts={s.symbol: s.cost_pct for s in long_signals},
            cost_cite_ids=cost_cite_ids,
            fetched_out=ltf_csvs,
        )
    if entry_tfs and short_signals:
        # Phase 7.6: the AI chart read needs the same chart ladder for shorts.
        from signaldesk.workflows.entry_refine import fetch_ltf_charts

        fetched_shorts = fetch_ltf_charts(short_signals, list(entry_tfs),
                                          tools.ohlcv, run_dir, bus, disclosures, charts)
        for sym, paths in fetched_shorts.items():
            ltf_csvs.setdefault(sym, {}).update(paths)
    if short_signals:
        disclosures.append(
            "Deterministic intraday refinement (Phase 7.5) is long-only; SHORT "
            "signals keep their daily plan unless the Phase 7.6 AI chart read "
            "chooses their entry.")

    # ---- Phase 7.6: AI chart read (vision entry selection) --------------------
    # The full chart ladder (1d -> 1m) for every chosen pair was streamed above;
    # a vision-capable LLM now picks the entry per signal. Stop geometry stays
    # risk-floored (R6); ledger records carry entry_mode="ai_chart_v1" (R7).
    from signaldesk.workflows.entry_refine import maybe_ai_chart_reads

    maybe_ai_chart_reads(
        signals, charts, run_dir, bus, registry, disclosures,
        demo_mode=demo_mode,
        price_cite_ids={s.symbol: price_cites[s.symbol] for s in signals if s.symbol in price_cites},
        atr_cite_ids={s.symbol: derived[s.symbol]["atr"] for s in signals if s.symbol in derived},
        atr_daily={s.symbol: feats[s.symbol].atr14 for s in signals if s.symbol in feats},
        cost_pcts={s.symbol: s.cost_pct for s in signals},
        cost_cite_ids=cost_cite_ids,
        ltf_csvs=ltf_csvs,
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

    # Regime-gate disclosures (item 32, trial T2 + declared short mirror)
    if not use_ai_engine and breakdowns:
        disclosures.append(
            "Regime gates (item 32 + short mirror, declared policy): longs refused "
            "below the 200d SMA, above "
            f"{scoring.REGIME_VOL_EXTREME_ANN_PCT:g}% annualized volatility, above "
            f"+{scoring.REGIME_MAX_7D_GAIN_PCT:g}% 7d gain, or more than "
            f"{scoring.REGIME_MAX_SMA20_DIST_PCT:g}% over SMA20; shorts refused by "
            "the same gates mirrored (above the 200d SMA, into a falling-knife 7d "
            "move, extended below SMA20, at the same volatility extreme). BTC "
            "below its own 200d SMA caps the long book, BTC above it caps the "
            "short book."
        )
    if not use_ai_engine:
        if market_regime_reason:
            disclosures.append(f"Regime gate active: {market_regime_reason}")
        if market_regime_reason_short:
            disclosures.append(f"Regime gate active: {market_regime_reason_short}")
    if low_history:
        disclosures.append(
            f"Regime gate: {low_history} candidate(s) lack 200d history — the "
            "trend gate is unevaluated for them, not assumed to pass (R4)."
        )

    # ---- Phase 8: critique & synthesis ----------------------------------------
    if use_ai_engine:
        preset_name = "ai-vision-v1"
    else:
        preset_name = (scoring.PRESET_NAME if profile.preset == "crypto"
                       else scoring.FX_PRESET_NAME)
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
        generator_model=(generator_model if use_ai_engine else ""),
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

    # ML shadow scores (learning layer): the saved model records P(r_net > 0)
    # per signal on the ledger record and gates nothing — acting on the score
    # requires a pre-registered trial that closed positive (rule R7).
    shadow_model = learn_mod.load_model(learn_mod.default_model_path(run_dir))
    if shadow_model is not None:
        scored, model_fp = learn_mod.apply_shadow_scores(ledger_records, shadow_model)
        if scored:
            bus.emit("P8", EventKind.RESULT,
                     f"ml shadow: {scored} signal(s) scored by model {model_fp} (shadow only)")

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
