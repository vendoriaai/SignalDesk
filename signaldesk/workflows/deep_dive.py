"""WF-2 Ticker Deep Dive (workflows.md).

TA sandbox -> fundamentals -> EDGAR annuals -> earnings -> web research
(bull/bear/catalysts) -> one-page cited dossier.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from signaldesk import costs as cost_model
from signaldesk import ledger as ledger_mod
from signaldesk.agent.events import EventBus, EventKind
from signaldesk.citations.registry import CitationRegistry
from signaldesk.report.render import to_markdown_deepdive
from signaldesk.report.schema import (
    DeepDiveReport,
    EarningsRow,
    EdgarAnnual,
    FundamentalsSnapshot,
    NewsClaim,
    Signal,
)
from signaldesk.sandbox.executor import run_script
from signaldesk.strategy import scoring

_SANDBOX_SCRIPT = """\
import json
from pathlib import Path

import pandas as pd

from signaldesk.analysis.charts import render_symbol_chart
from signaldesk.analysis.indicators import compute_features

for p in sorted(Path("input").glob("ohlcv_*.csv")):
    df = pd.read_csv(p)
    stem = p.stem[len("ohlcv_"):]
    symbol, tf = (stem.rsplit("_", 1) + ["1d"])[:2] if "_" in stem else (stem, "1d")
    feats = compute_features(df)
    pd.Series(feats).to_frame("value").to_csv(f"output/feats_{symbol}.csv")
    Path("output/charts").mkdir(parents=True, exist_ok=True)
    render_symbol_chart(df, symbol, Path(f"output/charts/{symbol}-{tf}.png"),
                        title_suffix=f"(deep dive, {tf})")
    print(json.dumps({symbol: {k: (None if v != v else v) for k, v in feats.items()
                                 if isinstance(v, (int, float))}}))
"""


class DeepDiveRequest(BaseModel):
    symbol: str
    market: str = "equities"  # equities | crypto
    with_research: bool = True
    entry_timeframes: list[str] | None = None  # None = default LTF set; [] = off


@dataclass
class DeepDiveToolSet:
    quotes: object
    ohlcv: object
    search: object
    fundamentals: object | None = None
    edgar: object | None = None
    earnings: object | None = None


@dataclass
class DeepDiveRun:
    report: DeepDiveReport
    report_md: str
    run_dir: Path


def _f(x):
    try:
        return float(x) if x is not None and not pd.isna(x) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def run_deep_dive(req: DeepDiveRequest, tools: DeepDiveToolSet, bus: EventBus, run_dir: Path) -> DeepDiveRun:
    symbol = req.symbol.upper()
    registry = CitationRegistry()
    disclosures: list[str] = []
    bus.emit("P0", EventKind.PHASE, f"deep dive: {symbol}")
    today = datetime.now(UTC).date().isoformat()

    # TA ------------------------------------------------------------------
    quote_res = tools.quotes.run(symbols=[symbol])
    bus.emit("P1", EventKind.TOOL, f"quotes: {quote_res.summary}")
    ohlcv_res = tools.ohlcv.run(symbol=symbol)
    bus.emit("P1", EventKind.TOOL, f"ohlcv: {ohlcv_res.summary}")

    quotes_df = pd.read_csv(quote_res.csv_files[0])
    qrow = quotes_df[quotes_df["symbol"] == symbol].iloc[0]
    price_cite = registry.register_direct(
        round(float(qrow["price"]), 8), f"{symbol} close",
        source_tool="quotes", file=Path(quote_res.csv_files[0]).name,
        row_key=symbol, column="price",
    )

    sandbox_dir = run_dir / "sandbox"
    input_dir = sandbox_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ohlcv_res.csv_files[0], input_dir / Path(ohlcv_res.csv_files[0]).name)
    proc = run_script(_SANDBOX_SCRIPT, sandbox_dir)
    bus.emit("P2", EventKind.SANDBOX, f"technical computation: {proc.stdout.strip()}")
    chart_path = sandbox_dir / "output" / "charts" / f"{symbol}-1d.png"
    chart_rel = ""
    if chart_path.exists():
        chart_rel = str(Path("sandbox") / "output" / "charts" / f"{symbol}-1d.png").replace("\\", "/")
        bus.emit("P2", EventKind.CHART, f"chart ready: {symbol} (1d)", path=chart_rel, symbol=symbol, tf="1d")
    feats_df = pd.read_csv(sandbox_dir / "output" / f"feats_{symbol}.csv", index_col=0)["value"]
    f = {k: _f(v) for k, v in feats_df.items() if k != "as_of"}
    feat = scoring.SymbolFeatures(
        close=f["close"], rsi14=f["rsi14"], atr14=f["atr14"], swing_low_20=f["swing_low_20"],
        above_sma20=bool(f.get("above_sma20")), above_sma50=bool(f.get("above_sma50")),
        ema9_above_ema21=bool(f.get("ema9_above_ema21")),
        macd_hist=f["macd_hist"], macd_hist_prev=f["macd_hist_prev"],
        volume=f["volume"], volume_avg30=f["volume_avg30"],
    )
    rsi_cite = registry.register_derived(
        round(f["rsi14"], 2), f"{symbol} RSI(14)",
        formula="RSI(close,14) Wilder", derived_from=[price_cite.id],
    )
    macd_cite = registry.register_derived(
        round(f["macd_hist"], 8), f"{symbol} MACD histogram",
        formula="EMA12-EMA26 minus signal(9)", derived_from=[price_cite.id],
    )
    atr_cite = registry.register_derived(
        round(f["atr14"], 8), f"{symbol} ATR(14)",
        formula="ATR(H,L,C,14) Wilder", derived_from=[price_cite.id],
    )
    score = scoring.score_symbol(feat, fear_greed=None)  # equities: no crypto sentiment
    technicals = {k: v for k, v in f.items() if isinstance(v, (int, float))}
    technicals.update(score=score.total, reasons=score.reasons)

    signal = None
    cost_pct = cost_model.round_trip_cost_pct(req.market, symbol)
    cost_cite = registry.register_direct(
        cost_pct, f"{symbol} assumed round-trip cost %",
        source_tool="cost_model", file="signaldesk/costs.py",
        row_key=symbol, column="round_trip_cost_pct",
    )
    if score.overextended:
        disclosures.append(f"RSI {f['rsi14']:.1f} >= 75 — overextended, no long signal (R3)")
    elif score.total >= scoring.SCORE_THRESHOLD:
        plan = scoring.build_trade_plan(f["close"], f["atr14"], f["swing_low_20"], cost_pct=cost_pct)
        risk_pct = (plan.entry - plan.stop) / plan.entry * 100.0 if plan.entry else 0.0
        signal = Signal(
            symbol=symbol, score=score.total,
            entry=round(plan.entry, 8), stop=round(plan.stop, 8),
            tp1=round(plan.tp1, 8), tp2=round(plan.tp2, 8),
            confluence=score.reasons + (
                [f"stop floored to keep cost at {cost_model.cost_in_r(cost_pct, risk_pct):.2f}R"]
                if plan.floored else []),
            catalysts=[],
            citations=[rsi_cite.id, macd_cite.id, atr_cite.id, price_cite.id, cost_cite.id],
            cost_pct=cost_pct,
            cost_in_r=round(cost_model.cost_in_r(cost_pct, risk_pct), 4),
        )
    bus.emit("P2", EventKind.RESULT, f"score {score.total:.0f}/100" + ("; signal" if signal else ""))

    # Phase 7.5: intraday entry refinement for the signal --------------------
    entry_charts: dict[str, dict[str, str]] = {}
    if signal:
        from signaldesk.markets import DEFAULT_ENTRY_TIMEFRAMES

        entry_tfs = (DEFAULT_ENTRY_TIMEFRAMES if req.entry_timeframes is None
                     else tuple(req.entry_timeframes))
        if entry_tfs:
            from signaldesk.workflows.entry_refine import refine_signal_entries

            refine_signal_entries(
                [signal], list(entry_tfs), tools.ohlcv, run_dir, bus, registry,
                disclosures, entry_charts,
                price_cite_ids={symbol: price_cite.id},
                atr_cite_ids={symbol: atr_cite.id},
                atr_daily={symbol: f["atr14"]},
                cost_pcts={symbol: cost_pct},
                cost_cite_ids={symbol: cost_cite.id},
            )

    # Fundamentals -----------------------------------------------------------
    fundamentals = FundamentalsSnapshot(name=symbol)
    if tools.fundamentals is not None:
        fres = tools.fundamentals.run(symbol=symbol)
        bus.emit("P3", EventKind.TOOL, f"fundamentals: {fres.summary}")
        if not fres.degraded and fres.csv_files:
            row = pd.read_csv(fres.csv_files[0]).iloc[0]

            def cite(col, label):
                if col in row.index and pd.notna(row[col]):
                    return registry.register_direct(
                        row[col].item() if hasattr(row[col], "item") else row[col],
                        label, source_tool="fundamentals",
                        file=Path(fres.csv_files[0]).name, row_key=symbol, column=col,
                    )
                return None

            fundamentals = FundamentalsSnapshot(
                name=str(row.get("longName") or symbol),
                sector=str(row.get("sector") or ""),
                industry=str(row.get("industry") or ""),
                market_cap=_f(row.get("marketCap")),
                trailing_pe=_f(row.get("trailingPE")),
                forward_pe=_f(row.get("forwardPE")),
                price_to_book=_f(row.get("priceToBook")),
                profit_margins=_f(row.get("profitMargins")),
                roe=_f(row.get("returnOnEquity")),
                revenue_growth=_f(row.get("revenueGrowth")),
                earnings_growth=_f(row.get("earningsGrowth")),
                recommendation=str(row.get("recommendationKey") or ""),
                target_price=_f(row.get("targetMeanPrice")),
                citations=[c.id for col, label in [
                    ("marketCap", f"{symbol} market cap"), ("trailingPE", f"{symbol} trailing PE"),
                    ("profitMargins", f"{symbol} profit margin"),
                ] if (c := cite(col, label)) is not None],
            )
    else:
        disclosures.append("Fundamentals provider not configured.")

    # EDGAR annuals ----------------------------------------------------------
    annuals: list[EdgarAnnual] = []
    if tools.edgar is not None and req.market == "equities":
        eres = tools.edgar.run(symbol=symbol)
        bus.emit("P4", EventKind.TOOL, f"edgar: {eres.summary}")
        if not eres.degraded and eres.csv_files:
            for row in pd.read_csv(eres.csv_files[0]).itertuples():
                annuals.append(EdgarAnnual(fy=str(row.fy), revenue=_f(row.revenue), net_income=_f(row.net_income)))
            if annuals:
                cite = registry.register_direct(
                    f"{len(annuals)} fiscal years", f"{symbol} EDGAR annuals",
                    source_tool="edgar_facts", file=Path(eres.csv_files[0]).name,
                    row_key=symbol, column="revenue,net_income",
                )
                fundamentals.citations.append(cite.id)
        else:
            disclosures.append("EDGAR annuals unavailable (degraded, disclosed per R4).")

    # Earnings ---------------------------------------------------------------
    earnings_rows: list[EarningsRow] = []
    earnings_next = ""
    if tools.earnings is not None:
        ires = tools.earnings.run(symbol=symbol)
        bus.emit("P4", EventKind.TOOL, f"earnings: {ires.summary}")
        if not ires.degraded and ires.csv_files:
            from signaldesk.tools.equities import EarningsTool  # noqa: F401 (keeps dep visible)
            edf = pd.read_csv(ires.csv_files[0])
            for row in edf.itertuples():
                earnings_rows.append(EarningsRow(
                    date=str(pd.Timestamp(row[1]).date()),
                    eps_estimate=_f(getattr(row, "eps_estimate", float("nan"))),
                    eps_actual=_f(getattr(row, "reported_eps", getattr(row, "eps_actual", float("nan")))),
                    surprise_pct=_f(getattr(row, "surprise_28pct_29", getattr(row, "surprise_pct", float("nan")))),
                ))
        if "next earnings" in ires.summary:
            earnings_next = ires.summary.split("next earnings", 1)[1].split(";")[0].strip()

    # Research: bull / bear / catalysts --------------------------------------
    bull: list[NewsClaim] = []
    bear: list[NewsClaim] = []
    cat: list[NewsClaim] = []
    if req.with_research and tools.search.available():
        base = symbol[:-3] if (req.market == "crypto" and symbol.endswith("USD")) else symbol
        name = fundamentals.name if fundamentals.name and fundamentals.name != symbol else base
        queries = [
            f"{name} bull case why buy {today[:7]}",
            f"{name} bear case risks {today[:7]}",
            f"{name} upcoming catalysts earnings news {today[:7]}",
        ]
        results = tools.search.batch(queries, max_results=3)
        for q, hits, sink in zip(queries, results.values(), (bull, bear, cat)):
            bus.emit("P5", EventKind.SEARCH, q, hits=len(hits))
            for h in hits[:1]:
                sink.append(NewsClaim(claim=h.snippet or h.title, url=h.url, published=h.published))
    elif req.with_research:
        disclosures.append("Web research unavailable (no search provider); dossier is data-only.")

    # Assemble ---------------------------------------------------------------
    bits = []
    if score.total >= scoring.SCORE_THRESHOLD:
        bits.append(f"technically constructive (score {score.total:.0f}/100)")
    if fundamentals.revenue_growth and fundamentals.revenue_growth == fundamentals.revenue_growth:
        gr = fundamentals.revenue_growth * 100
        bits.append(f"revenue growth {gr:+.1f}%")
    if fundamentals.recommendation:
        bits.append(f"analyst consensus {fundamentals.recommendation}")
    summary = f"{fundamentals.name or symbol}: " + (", ".join(bits) if bits else "mixed picture; see sections.")
    report = DeepDiveReport(
        symbol=symbol,
        as_of=datetime.now(UTC).isoformat(timespec="seconds"),
        name=fundamentals.name,
        summary=summary,
        technicals=technicals,
        chart=chart_rel,
        charts=entry_charts,
        signal=signal,
        fundamentals=fundamentals,
        earnings_history=earnings_rows[-8:],
        earnings_next=earnings_next,
        annuals=annuals,
        bull_case=bull,
        bear_case=bear,
        catalysts=cat,
        disclosures=disclosures,
        citations=registry.all(),
    )
    bus.emit("P6", EventKind.RESULT, f"dossier ready: {len(registry)} citations")

    # Ledger: freeze the emitted signal for later outcome scoring -------------
    if signal is not None:
        bars_last = ""
        try:
            bars_last = str(pd.read_csv(ohlcv_res.csv_files[0])["date"].iloc[-1])
        except Exception:
            pass
        records = ledger_mod.record_report(
            report, run_dir.name, market=req.market,
            data_hashes={symbol: ledger_mod.sha256_file(ohlcv_res.csv_files[0])},
            bars_last_dates={symbol: bars_last},
            cost_pcts={symbol: cost_pct},
            demo=tools.ohlcv.__class__.__name__.startswith("Demo"),
            signals=[signal],
        )
        ledger_file = ledger_mod.default_ledger_path(run_dir)
        written = ledger_mod.append_records(ledger_file, records)
        if written:
            bus.emit("P6", EventKind.RESULT,
                     f"ledger: {written} signal(s) recorded -> {ledger_file}")

    run_dir.mkdir(parents=True, exist_ok=True)
    md = to_markdown_deepdive(report)
    (run_dir / "report.md").write_text(md, encoding="utf-8")
    import json as _json

    (run_dir / "report.json").write_text(
        _json.dumps(report.model_dump(mode="json"), indent=2, default=str), encoding="utf-8"
    )
    (run_dir / "trace.jsonl").write_text(bus.to_jsonl(), encoding="utf-8")
    return DeepDiveRun(report=report, report_md=md, run_dir=run_dir)
