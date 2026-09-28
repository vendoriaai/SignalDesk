"""SignalDesk CLI (M1/M2 headless surface).

    signaldesk scan crypto --demo        offline deterministic scan
    signaldesk scan crypto               live market scan (WF-1)
    signaldesk scan forex                FX majors scan (WF-3; macro via FRED/yfinance)
    signaldesk scan metals               gold/silver scan (WF-3)
    signaldesk deepdive AAPL             one-page cited dossier (WF-2)
    signaldesk watchlist add growth BTCUSD ETHUSD --market crypto
    signaldesk scan watchlist:growth     WF-4 watchlist scan with diff vs last run
    signaldesk ask "deep dive AAPL before earnings"
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from signaldesk.agent.events import EventBus
from signaldesk.config import Config
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

app = typer.Typer(add_completion=False, help="SignalDesk — AI market research & signal agent.")

wl_app = typer.Typer(help="Manage local watchlists (WF-4).")
app.add_typer(wl_app, name="watchlist")

paper_app = typer.Typer(help="Log paper-trade executions against ledger signals (WF-5).")
app.add_typer(paper_app, name="paper")

trial_app = typer.Typer(help="Pre-register signal-generation changes and judge them (roadmap item 33).")
app.add_typer(trial_app, name="trial")


# --------------------------------------------------------------------------
# tool factories
# --------------------------------------------------------------------------

def _live_tools(config: Config, market: str, artifacts_dir: Path) -> ToolSet:
    from signaldesk.tools.search import SearchTool

    search = SearchTool(tavily_api_key=config.tavily_api_key)
    if market == "crypto":
        from signaldesk.tools.coingecko import CoinGeckoMoversTool
        from signaldesk.tools.sentiment import AltSeasonTool, FearGreedTool
        from signaldesk.tools.yfinance_tools import YFinanceOHLCVTool, YFinanceQuotesTool

        return ToolSet(
            movers=CoinGeckoMoversTool(artifacts_dir),
            quotes=YFinanceQuotesTool(artifacts_dir, market=market),
            ohlcv=YFinanceOHLCVTool(artifacts_dir, market=market),
            search=search,
            fear_greed=FearGreedTool(artifacts_dir),
            altseason=AltSeasonTool(artifacts_dir),
        )
    from signaldesk.tools.alphavantage import AlphaVantageFXTool
    from signaldesk.tools.fx_universe import StaticMetalsTool, YFinanceFXMoversTool
    from signaldesk.tools.macro import MacroSnapshotTool
    from signaldesk.tools.yfinance_tools import YFinanceOHLCVTool, YFinanceQuotesTool

    macro = MacroSnapshotTool(artifacts_dir, fred_api_key=config.fred_api_key)
    if market == "forex":
        if config.alphavantage_api_key:
            ohlcv = AlphaVantageFXTool(artifacts_dir, config.alphavantage_api_key)
        else:
            ohlcv = YFinanceOHLCVTool(artifacts_dir, market="forex")
        return ToolSet(
            movers=YFinanceFXMoversTool(artifacts_dir),
            quotes=YFinanceQuotesTool(artifacts_dir, market="forex"),
            ohlcv=ohlcv,
            search=search,
            macro=macro,
        )
    if market == "metals":
        return ToolSet(
            movers=StaticMetalsTool(artifacts_dir),
            quotes=YFinanceQuotesTool(artifacts_dir, market="metals"),
            ohlcv=YFinanceOHLCVTool(artifacts_dir, market="metals"),
            search=search,
            macro=macro,
        )
    raise typer.BadParameter(f"no live adapter set for market '{market}' yet")


def _demo_tools(market: str, artifacts_dir: Path) -> ToolSet:
    from signaldesk.tools import demo

    if market == "crypto":
        return ToolSet(
            movers=demo.DemoMoversTool(artifacts_dir, market),
            quotes=demo.DemoQuotesTool(artifacts_dir, market),
            ohlcv=demo.DemoOHLCVTool(artifacts_dir, market),
            search=demo.DemoSearchTool(),
            fear_greed=demo.DemoFearGreedTool(artifacts_dir),
            altseason=demo.DemoAltSeasonTool(artifacts_dir),
        )
    return ToolSet(
        movers=demo.DemoMoversTool(artifacts_dir, market),
        quotes=demo.DemoQuotesTool(artifacts_dir, market),
        ohlcv=demo.DemoOHLCVTool(artifacts_dir, market),
        search=demo.DemoSearchTool(),
        macro=demo.DemoMacroTool(artifacts_dir, market),
    )


def _echo_report(report_md: str, run_dir: Path, json_out: bool, report_json: dict) -> None:
    if json_out:
        typer.echo(json.dumps(report_json, indent=2, default=str))
    else:
        typer.echo(report_md)
    typer.secho(f"\nrun dir: {run_dir}", dim=True)
    typer.secho(f"report:  {run_dir / 'report.md'}", dim=True)
    from signaldesk import ledger as ledger_mod

    ledger_file = ledger_mod.default_ledger_path(run_dir)
    if ledger_file.is_file():
        typer.secho(f"ledger:  {ledger_file}", dim=True)


def _parse_entry_tf(entry_tf: str) -> list[str] | None:
    """'' -> workflow default; 'off' -> disabled; '30m,15m' -> custom list."""
    value = (entry_tf or "").strip()
    if not value:
        return None
    if value.lower() in ("off", "none"):
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the SignalDesk version."""
    import signaldesk

    typer.echo(f"signaldesk {signaldesk.__version__}")


@app.command()
def scan(
    market: str = typer.Argument("crypto", help="crypto | forex | metals — or watchlist:<name>."),
    demo: bool = typer.Option(False, "--demo", help="Offline deterministic scan (no network)."),
    universe_size: int = typer.Option(12, "--universe-size", "-n", min=2, max=25),
    min_score: float = typer.Option(60.0, "--min-score", min=0, max=100),
    trace: bool = typer.Option(False, "--trace", help="Stream execution events as JSONL."),
    json_out: bool = typer.Option(False, "--json", help="Print report as JSON instead of Markdown."),
    entry_tf: str = typer.Option("", "--entry-tf",
                                 help="Comma list of intraday entry timeframes "
                                      "(default: 30m,15m,5m,1m). 'off' disables."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override SignalDesk home."),
) -> None:
    """Run a market scan (WF-1 / WF-3 / WF-4)."""
    config = Config.from_env()
    if data_dir:
        config.data_dir = data_dir

    entry_timeframes = _parse_entry_tf(entry_tf)

    watchlist_name = None
    universe_override = None
    if market.startswith("watchlist:"):
        from signaldesk import watchlists

        watchlist_name = market.split(":", 1)[1]
        wl = watchlists.load(config.data_dir, watchlist_name)
        if not wl.symbols:
            typer.secho(f"watchlist '{watchlist_name}' is empty", err=True, fg=typer.colors.RED)
            raise typer.Exit(code=1)
        universe_override, market = wl.symbols, wl.market

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + (f"-wl-{watchlist_name}" if watchlist_name else "") + ("-demo" if demo else "")
    run_dir = config.run_dir(run_id)
    tools = _demo_tools(market, run_dir / "artifacts") if demo else _live_tools(config, market, run_dir / "artifacts")

    def _sink(event) -> None:
        if trace:
            typer.echo(event.model_dump_json())

    bus = EventBus(sink=_sink if trace else None)
    try:
        run = run_market_scan(
            MarketScanRequest(market=market, universe_size=universe_size, min_score=min_score,
                              universe_override=universe_override, watchlist_name=watchlist_name,
                              entry_timeframes=entry_timeframes),
            tools, bus, run_dir,
        )
    except Exception as exc:
        typer.secho(f"scan failed: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    # WF-4 diff section (what changed since last scan of this watchlist)
    if watchlist_name:
        from signaldesk import watchlists

        wl = watchlists.load(config.data_dir, watchlist_name)
        diff_lines = _watchlist_diff(wl, run.report)
        if diff_lines:
            run.report_md = run.report_md.replace(
                "## Ranked signals", "## Watchlist diff\n" + "\n".join(diff_lines) + "\n\n## Ranked signals",
                1,
            )
        wl.last_scan_report = str(run.run_dir / "report.json")
        watchlists.save(config.data_dir, wl)

    _echo_report(run.report_md, run.run_dir, json_out, run.report.model_dump(mode="json"))


def _watchlist_diff(wl, report) -> list[str]:
    import json as _json

    from signaldesk.report.schema import ScanReport

    if not wl.last_scan_report:
        return []
    if not Path(wl.last_scan_report).exists():
        return ["- previous report file missing; no diff"]
    try:
        prev = ScanReport(**_json.loads(Path(wl.last_scan_report).read_text(encoding="utf-8")))
    except Exception:
        return ["- previous report unreadable; no diff"]
    now_sig = {s.symbol: s for s in report.signals}
    prev_sig = {s.symbol: s for s in prev.signals}
    lines = []
    for sym in now_sig:
        d = f" (was {prev_sig[sym].score:.0f})" if sym in prev_sig and prev_sig[sym].score != now_sig[sym].score else ""
        lines.append(f"- {'REPEAT' if sym in prev_sig else 'NEW'} signal: {sym} score {now_sig[sym].score:.0f}{d}")
    for sym in prev_sig:
        if sym not in now_sig:
            lines.append(f"- DROPPED signal: {sym} (was {prev_sig[sym].score:.0f})")
    return lines or ["- no signal changes vs previous scan"]


@app.command()
def deepdive(
    symbol: str = typer.Argument(..., help="Ticker to research, e.g. AAPL."),
    market: str = typer.Option("equities", "--market", "-m", help="equities | crypto"),
    demo: bool = typer.Option(False, "--demo", help="Offline deterministic run."),
    no_research: bool = typer.Option(False, "--no-research", help="Skip web research phases."),
    entry_tf: str = typer.Option("", "--entry-tf",
                                 help="Comma list of intraday entry timeframes "
                                      "(default: 30m,15m,5m,1m). 'off' disables."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run WF-2: one-page cited dossier on a single ticker."""
    from signaldesk.workflows.deep_dive import DeepDiveRequest, DeepDiveToolSet, run_deep_dive

    config = Config.from_env()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-dd-{symbol}" + ("-demo" if demo else "")
    run_dir = config.run_dir(run_id)

    if demo:
        from signaldesk.tools import demo, demo_equities

        tools = DeepDiveToolSet(
            quotes=demo.DemoQuotesTool(run_dir / "artifacts", market),
            ohlcv=demo.DemoOHLCVTool(run_dir / "artifacts", market),
            search=demo.DemoSearchTool(),
            fundamentals=(demo_equities.DemoFundamentalsTool(run_dir / "artifacts") if market == "equities" else None),
            edgar=(demo_equities.DemoEdgarTool(run_dir / "artifacts") if market == "equities" else None),
            earnings=(demo_equities.DemoEarningsTool(run_dir / "artifacts") if market == "equities" else None),
        )
    else:
        from signaldesk.tools.equities import EarningsTool, EdgarFactsTool, FundamentalsTool
        from signaldesk.tools.search import SearchTool
        from signaldesk.tools.yfinance_tools import YFinanceOHLCVTool, YFinanceQuotesTool

        tools = DeepDiveToolSet(
            quotes=YFinanceQuotesTool(run_dir / "artifacts", market),
            ohlcv=YFinanceOHLCVTool(run_dir / "artifacts", market),
            search=SearchTool(tavily_api_key=config.tavily_api_key),
            fundamentals=(FundamentalsTool(run_dir / "artifacts") if market == "equities" else None),
            edgar=(EdgarFactsTool(run_dir / "artifacts") if market == "equities" else None),
            earnings=(EarningsTool(run_dir / "artifacts") if market == "equities" else None),
        )

    bus = EventBus()
    try:
        run = run_deep_dive(DeepDiveRequest(symbol=symbol, market=market, with_research=not no_research,
                                            entry_timeframes=_parse_entry_tf(entry_tf)),
                            tools, bus, run_dir)
    except Exception as exc:
        typer.secho(f"deep dive failed: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _echo_report(run.report_md, run.run_dir, json_out, run.report.model_dump(mode="json"))


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Natural-language task, e.g. 'deep dive AAPL before earnings'."),
    demo: bool = typer.Option(False, "--demo"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Plan and execute a workflow from a natural-language prompt."""
    from signaldesk.agent.planner import classify

    config = Config.from_env()
    plan = classify(prompt, openai_key=config.openai_api_key, anthropic_key=config.anthropic_api_key,
                    openrouter_key=config.openrouter_api_key, model=config.llm_model)
    typer.echo(f"plan: workflow={plan.workflow.value} market={plan.market}"
               + (f" symbol={plan.symbol}" if plan.symbol else "")
               + (f" watchlist={plan.watchlist}" if plan.watchlist else "")
               + f" ({plan.note})")

    from signaldesk.agent.planner import Workflow

    if plan.workflow in (Workflow.MARKET_SCAN, Workflow.FOREX_SCAN):
        target = plan.market
        _invoke_scan(target, demo, json_out, config)
    elif plan.workflow == Workflow.WATCHLIST_SCAN:
        _invoke_scan(f"watchlist:{plan.watchlist}", demo, json_out, config)
    elif plan.workflow == Workflow.DEEP_DIVE:
        if not plan.symbol:
            typer.secho("planner: deep dive needs a symbol", err=True, fg=typer.colors.RED)
            raise typer.Exit(code=1)
        _invoke_deepdive(plan.symbol, plan.market, demo, json_out)


def _invoke_scan(market: str, demo: bool, json_out: bool, config: Config) -> None:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ("-demo" if demo else "")
    watchlist_name = None
    universe_override = None
    if market.startswith("watchlist:"):
        from signaldesk import watchlists

        watchlist_name = market.split(":", 1)[1]
        wl = watchlists.load(config.data_dir, watchlist_name)
        universe_override, market = wl.symbols, wl.market
    run_dir = config.run_dir(run_id)
    tools = _demo_tools(market, run_dir / "artifacts") if demo else _live_tools(config, market, run_dir / "artifacts")
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market=market, universe_override=universe_override, watchlist_name=watchlist_name),
        tools, bus, run_dir,
    )
    _echo_report(run.report_md, run.run_dir, json_out, run.report.model_dump(mode="json"))


def _invoke_deepdive(symbol: str, market: str, demo: bool, json_out: bool) -> None:
    from signaldesk.agent.events import EventBus as _Bus

    config = Config.from_env()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-dd-{symbol}" + ("-demo" if demo else "")
    run_dir = config.run_dir(run_id)
    from signaldesk.workflows.deep_dive import DeepDiveRequest, DeepDiveToolSet, run_deep_dive

    if demo:
        from signaldesk.tools import demo as d, demo_equities as de

        toolset = DeepDiveToolSet(
            quotes=d.DemoQuotesTool(run_dir / "artifacts", market),
            ohlcv=d.DemoOHLCVTool(run_dir / "artifacts", market),
            search=d.DemoSearchTool(),
            fundamentals=(de.DemoFundamentalsTool(run_dir / "artifacts") if market == "equities" else None),
            edgar=(de.DemoEdgarTool(run_dir / "artifacts") if market == "equities" else None),
            earnings=(de.DemoEarningsTool(run_dir / "artifacts") if market == "equities" else None),
        )
    else:
        from signaldesk.tools.equities import EarningsTool, EdgarFactsTool, FundamentalsTool
        from signaldesk.tools.search import SearchTool
        from signaldesk.tools.yfinance_tools import YFinanceOHLCVTool, YFinanceQuotesTool

        toolset = DeepDiveToolSet(
            quotes=YFinanceQuotesTool(run_dir / "artifacts", market),
            ohlcv=YFinanceOHLCVTool(run_dir / "artifacts", market),
            search=SearchTool(tavily_api_key=config.tavily_api_key),
            fundamentals=(FundamentalsTool(run_dir / "artifacts") if market == "equities" else None),
            edgar=(EdgarFactsTool(run_dir / "artifacts") if market == "equities" else None),
            earnings=(EarningsTool(run_dir / "artifacts") if market == "equities" else None),
        )
    run = run_deep_dive(DeepDiveRequest(symbol=symbol, market=market), toolset, _Bus(), run_dir)
    _echo_report(run.report_md, run.run_dir, json_out, run.report.model_dump(mode="json"))


# --------------------------------------------------------------------------
# watchlist commands
# --------------------------------------------------------------------------

@wl_app.command("add")
def wl_add(
    name: str,
    symbols: list[str],
    market: str = typer.Option("crypto", "--market", "-m"),
) -> None:
    """Add symbols to a watchlist (created on first use)."""
    from signaldesk import watchlists

    config = Config.from_env()
    wl = watchlists.add_symbols(config.data_dir, name, symbols, market=market)
    typer.echo(f"{wl.name} [{wl.market}]: {', '.join(wl.symbols)}")


@wl_app.command("remove")
def wl_remove(name: str, symbols: list[str]) -> None:
    """Remove symbols from a watchlist."""
    from signaldesk import watchlists

    config = Config.from_env()
    wl = watchlists.remove_symbols(config.data_dir, name, symbols)
    typer.echo(f"{wl.name}: {', '.join(wl.symbols) or '(empty)'}")


@wl_app.command("list")
def wl_list() -> None:
    """List all watchlists."""
    from signaldesk import watchlists

    config = Config.from_env()
    for wl in watchlists.list_all(config.data_dir):
        typer.echo(f"{wl.name} [{wl.market}] ({len(wl.symbols)}): {', '.join(wl.symbols)}")


@app.command(name="sandbox-exec", hidden=True)
def _sandbox_exec(path: str) -> None:
    """INTERNAL: child-process entry used by the sandbox inside frozen builds."""
    import runpy
    import sys as _sys

    _sys.argv = [path]
    runpy.run_path(path, run_name="__main__")


@app.command()
def outcomes(
    horizon: int = typer.Option(14, "--horizon", min=1, max=250,
                                help="Time barrier, in daily bars after the signal."),
    market: str = typer.Option("", "--market", "-m",
                               help="Only signals from this market (crypto|forex|metals|equities)."),
    demo: bool = typer.Option(False, "--demo", help="Resolve against the offline demo feed."),
    include_demo: bool = typer.Option(False, "--include-demo",
                                      help="Include signals generated from demo data in the stats."),
    backfill: bool = typer.Option(False, "--backfill",
                                  help="Import signals from past runs/*/report.json first."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Score recorded signals against subsequent bars (expectancy in R, net of cost)."""
    from signaldesk import metrics as metrics_mod
    from signaldesk import outcomes as outcomes_mod
    from signaldesk import resolver as resolver_mod
    from signaldesk import universe as universe_mod

    config = Config.from_env()
    records = resolver_mod.select_records(
        config.data_dir, market=market or None, demo=demo, include_demo=include_demo,
        backfill=backfill,
        on_backfill=lambda n: typer.secho(f"backfilled {n} signal(s) from previous runs",
                                          dim=True))
    if not records:
        typer.secho("no scored signals yet — run a scan first (signaldesk scan crypto)",
                    err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)

    progress = (lambda text: typer.secho(text, dim=True)) if trace_events() else None
    rows = resolver_mod.run_resolution(
        config.data_dir, records=records, demo=demo,
        horizon_bars=horizon, on_progress=progress)

    summary = metrics_mod.summarize(rows)
    bias_note = universe_mod.bias_note(config.data_dir)
    if bias_note:
        summary.notes.append(bias_note)
    if json_out:
        import dataclasses

        typer.echo(json.dumps({"metrics": dataclasses.asdict(summary), "resolutions": rows},
                              indent=2, default=str))
    else:
        typer.echo(metrics_mod.render_text(summary))
        typer.secho(f"\noutcomes: {outcomes_mod.outcomes_path(config.data_dir)}", dim=True)


def trace_events() -> bool:
    """Verbose per-symbol progress for the outcomes command (env: SIGNALDESK_TRACE=1)."""
    import os

    return os.environ.get("SIGNALDESK_TRACE", "").strip() not in ("", "0", "false")


@app.command(name="ledger")
def ledger_cmd(
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show what the signal ledger holds (the input to `signaldesk outcomes`)."""
    from signaldesk import ledger as ledger_mod

    config = Config.from_env()
    ledger_file = ledger_mod.ledger_path(config.data_dir)
    records = ledger_mod.read_records(ledger_file)
    if not records:
        typer.secho(f"ledger empty or missing: {ledger_file}", dim=True)
        typer.echo("run `signaldesk scan crypto` (or `signaldesk outcomes --backfill`) first")
        return
    by_market: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    for rec in records:
        by_market[rec.market] = by_market.get(rec.market, 0) + 1
        by_mode[rec.entry_mode] = by_mode.get(rec.entry_mode, 0) + 1
    stamps = sorted(r.created_at for r in records if r.created_at)
    payload = {
        "path": str(ledger_file),
        "signals": len(records),
        "demo": sum(1 for r in records if r.demo),
        "backfilled": sum(1 for r in records if r.source == "backfill"),
        "first": stamps[0] if stamps else "",
        "last": stamps[-1] if stamps else "",
        "by_market": by_market,
        "by_entry_mode": by_mode,
        "symbols": sorted({r.symbol for r in records}),
        "weights_hashes": sorted({r.weights_hash for r in records if r.weights_hash}),
    }
    if json_out:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"ledger: {payload['path']}")
    typer.echo(f"signals: {payload['signals']} ({payload['demo']} demo, "
               f"{payload['backfilled']} backfilled) · {payload['first']} .. {payload['last']}")
    typer.echo("by market: " + ", ".join(f"{k} {v}" for k, v in sorted(by_market.items())))
    typer.echo("by entry mode: " + ", ".join(f"{k} {v}" for k, v in sorted(by_mode.items())))
    typer.echo(f"rule fingerprints: {', '.join(payload['weights_hashes']) or '(none)'}")


@app.command()
def universe(
    market: str = typer.Option("", "--market", "-m",
                               help="Audit only this market (crypto|forex|metals|equities)."),
    demo: bool = typer.Option(False, "--demo", help="Classify against the offline demo feed."),
    stale_days: int = typer.Option(30, "--stale-days", min=1, max=365,
                                   help="No fresh bar in N days -> dormant."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Size the survivorship bias in the ledger sample (point-in-time universe audit)."""
    from signaldesk import universe as universe_mod

    config = Config.from_env()
    audit = universe_mod.run_audit(
        config.data_dir, market=market or None, demo=demo, stale_days=stale_days,
        on_progress=(lambda t: typer.secho(t, dim=True)) if trace_events() else None)
    universe_mod.save_audit(config.data_dir, audit)
    if json_out:
        import dataclasses

        typer.echo(json.dumps(dataclasses.asdict(audit), indent=2, default=str))
    else:
        typer.echo(universe_mod.render_audit_text(audit))
        typer.secho(f"\naudit saved: {universe_mod.audit_path(config.data_dir)}", dim=True)


# --------------------------------------------------------------------------
# paper-trading commands (WF-5: execution-vs-model gap)
# --------------------------------------------------------------------------

@paper_app.command("fill")
def paper_fill(
    signal_id: str,
    price: float = typer.Option(..., "--price", "-p", help="Actual (paper) fill price."),
    note: str = typer.Option("", "--note", "-n"),
) -> None:
    """Record that a ledger signal was taken, at the price actually filled."""
    from signaldesk import paper as paper_mod

    try:
        ev = paper_mod.log_event(Config.from_env().data_dir, signal_id, "fill",
                                 price=price, note=note)
    except KeyError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"fill recorded: {ev.signal_id} @ {ev.price}")


@paper_app.command("miss")
def paper_miss(
    signal_id: str,
    note: str = typer.Option("", "--note", "-n"),
) -> None:
    """Record that a ledger signal was NOT taken (skipped or never filled)."""
    from signaldesk import paper as paper_mod

    try:
        ev = paper_mod.log_event(Config.from_env().data_dir, signal_id, "miss", note=note)
    except KeyError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"miss recorded: {ev.signal_id}")


@paper_app.command("list")
def paper_list() -> None:
    """Show paper decisions and the execution-vs-model gap so far."""
    from signaldesk import outcomes as outcomes_mod
    from signaldesk import paper as paper_mod

    config = Config.from_env()
    records = {r.signal_id: r for r in ledger_read(config)}
    first = paper_mod.first_events(config.data_dir)
    if not first:
        typer.echo("no paper events — log one with `signaldesk paper fill <signal_id> --price P`")
        return
    latest = outcomes_mod.load_latest(outcomes_mod.outcomes_path(config.data_dir))
    for ev in first.values():
        rec = records.get(ev.signal_id)
        sym = rec.symbol if rec else "?"
        gap = ""
        if ev.event == "fill" and ev.price is not None and rec:
            gap = f" gap {paper_mod.entry_gap_r(rec, ev.price):+.2f}R"
        status = latest.get(ev.signal_id, {}).get("status", "open")
        typer.echo(f"{ev.event:4} {sym:12} {status:4} {ev.signal_id}{gap}")
    stats = paper_mod.paper_stats(config.data_dir, records, latest)
    typer.echo(f"\nfills {stats['fills']} · misses {stats['misses']} · "
               f"fill rate {stats['fill_rate']:.0%} · "
               f"mean entry gap {stats['mean_entry_gap_r']:+.2f}R (n={stats['n_entry_gap']})")


def ledger_read(config: Config) -> list:
    from signaldesk import ledger as ledger_mod

    return ledger_mod.read_records(ledger_mod.ledger_path(config.data_dir))


@trial_app.command("add")
def trial_add(
    name: str = typer.Option(..., "--name", "-n", help="Short trial name."),
    hypothesis: str = typer.Option(..., "--hypothesis", "-H",
                                   help="What you believe will happen and why."),
    change: str = typer.Option(..., "--change", "-c", help="Exactly what changed."),
    judging: str = typer.Option(..., "--judging", "-j",
                                help="Pre-committed criteria (metric, threshold, comparison)."),
    min_signals: int = typer.Option(30, "--min-signals", min=1),
    min_weeks: int = typer.Option(4, "--min-weeks", min=1),
) -> None:
    """Pre-register a rule change BEFORE it starts producing signals (R7)."""
    from signaldesk import trials as trials_mod

    trial = trials_mod.log_trial(
        Config.from_env().data_dir, name=name, hypothesis=hypothesis,
        change=change, judging=judging, min_signals=min_signals,
        min_weeks=min_weeks)
    typer.echo(f"registered {trial['id']}: {trial['name']}")
    typer.echo("criteria are frozen now — judge it on `signaldesk outcomes` when the minimum is met")


@trial_app.command("list")
def trial_list() -> None:
    """Show the trial log (running and closed trials)."""
    from signaldesk import trials as trials_mod

    typer.echo(trials_mod.render_text(trials_mod.read_trials(Config.from_env().data_dir)))


@trial_app.command("close")
def trial_close(
    trial_id: str = typer.Argument(..., help="Trial id, e.g. T1."),
    outcome: str = typer.Option(..., "--outcome", "-o",
                                help="The pre-committed judgement, against the logged criteria."),
) -> None:
    """Close a trial with its outcome (the declared criteria are never edited)."""
    from signaldesk import trials as trials_mod

    closed = trials_mod.close_trial(Config.from_env().data_dir, trial_id, outcome)
    if closed is None:
        typer.secho(f"no trial '{trial_id}'", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"closed {closed['id']}: {closed['outcome']}")


@app.command()
def serve(
    port: int = typer.Option(8787, "--port", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
) -> None:
    """Start the local API + UI server (browser UI at http://127.0.0.1:PORT)."""
    from signaldesk import api

    typer.echo(f"SignalDesk UI on http://{host}:{port}")
    api.main(port=port, host=host)


@app.command()
def desktop(
    port: int = typer.Option(8787, "--port", "-p"),
) -> None:
    """Launch the desktop app (pywebview window over the local server)."""
    from signaldesk.desktop import launch

    launch(port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
