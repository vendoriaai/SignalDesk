"""Eval harness (roadmap Phase 2.16): run evals/cases.json against the
rules-only planner and, for run=true cases, execute the workflow on the
offline demo market and validate the report contract.

Usage:
    python -m signaldesk.evals.runner [--root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from signaldesk.agent.events import EventBus
from signaldesk.agent.planner import classify_rules
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

ROOT = Path(__file__).resolve().parents[2] / "evals"


@dataclass
class EvalResult:
    case_id: int
    prompt: str
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    detail: str = ""


def _check_plan(plan, expect: dict) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    if "workflow" in expect:
        checks["workflow"] = plan.workflow.value in expect["workflow"]
    if "market" in expect:
        checks["market"] = plan.market in expect["market"]
    if "symbol" in expect:
        checks["symbol"] = plan.symbol in expect["symbol"]
    if "watchlist" in expect:
        checks["watchlist"] = plan.watchlist in expect["watchlist"]
    return checks


def _run_demo_scan(plan, run_dir: Path) -> tuple[bool, str]:
    from signaldesk.tools import demo as d

    market = plan.market if plan.market in ("crypto", "forex", "metals") else "crypto"
    if market == "crypto":
        tools = ToolSet(
            movers=d.DemoMoversTool(run_dir, market), quotes=d.DemoQuotesTool(run_dir, market),
            ohlcv=d.DemoOHLCVTool(run_dir, market), search=d.DemoSearchTool(),
            fear_greed=d.DemoFearGreedTool(run_dir), altseason=d.DemoAltSeasonTool(run_dir),
        )
    else:
        tools = ToolSet(
            movers=d.DemoMoversTool(run_dir, market), quotes=d.DemoQuotesTool(run_dir, market),
            ohlcv=d.DemoOHLCVTool(run_dir, market), search=d.DemoSearchTool(),
            macro=d.DemoMacroTool(run_dir, market),
        )
    bus = EventBus()
    try:
        run = run_market_scan(MarketScanRequest(market=market, universe_size=8), tools, bus, run_dir)
    except Exception as exc:
        return False, f"run failed: {exc}"
    cov = run.report.citation_coverage
    if cov < 1.0:
        return False, f"citation coverage {cov:.0%}"
    if len(run.report.citations) < 10:
        return False, "too few citations"
    return True, f"{len(run.report.signals)} signals, {len(run.report.citations)} citations"


def _run_demo_deepdive(symbol: str, run_dir: Path) -> tuple[bool, str]:
    from signaldesk.tools import demo as d
    from signaldesk.tools import demo_equities as de
    from signaldesk.workflows.deep_dive import DeepDiveRequest, DeepDiveToolSet, run_deep_dive

    tools = DeepDiveToolSet(
        quotes=d.DemoQuotesTool(run_dir, "equities"), ohlcv=d.DemoOHLCVTool(run_dir, "equities"),
        search=d.DemoSearchTool(), fundamentals=de.DemoFundamentalsTool(run_dir),
        edgar=de.DemoEdgarTool(run_dir), earnings=de.DemoEarningsTool(run_dir),
    )
    try:
        run = run_deep_dive(DeepDiveRequest(symbol=symbol), tools, EventBus(), run_dir)
    except Exception as exc:
        return False, f"deep dive failed: {exc}"
    if not run.report.citations:
        return False, "no citations"
    if not run.report.technicals:
        return False, "no technicals"
    return True, f"score {run.report.technicals.get('score')}, {len(run.report.citations)} citations"


def evaluate(root: Path | None = None) -> tuple[list[EvalResult], float]:
    root = root or ROOT
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))["cases"]
    tmp = Path(tempfile.mkdtemp(prefix="signaldesk-eval-"))
    results: list[EvalResult] = []
    for case in cases:
        plan = classify_rules(case["prompt"])
        checks = _check_plan(plan, case["expect"])
        detail = f"-> {plan.workflow.value}/{plan.market}"
        if case.get("symbol"):
            detail += f" symbol={plan.symbol}"
        if case.get("watchlist"):
            detail += f" wl={plan.watchlist}"
        if case.get("run"):
            run_dir = tmp / f"case-{case['id']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            if plan.workflow.value == "deep_dive" and plan.symbol:
                checks["run"], detail2 = _run_demo_deepdive(plan.symbol, run_dir)
            elif plan.workflow.value in ("market_scan", "forex_scan"):
                checks["run"], detail2 = _run_demo_scan(plan, run_dir)
            else:
                checks["run"], detail2 = False, f"no runner for {plan.workflow.value}"
            detail += f"; {detail2}"
        ok = all(checks.values())
        results.append(EvalResult(case_id=case["id"], prompt=case["prompt"], ok=ok,
                                  checks=checks, detail=detail))
    passed = sum(r.ok for r in results)
    return results, passed / len(results)


def main() -> None:
    parser = argparse.ArgumentParser(description="SignalDesk eval harness")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results, score = evaluate(args.root)
    if args.json:
        print(json.dumps({"pass": score, "results": [r.__dict__ for r in results]}, indent=2))
        return
    for r in results:
        flag = "PASS" if r.ok else "FAIL"
        bad = [k for k, v in r.checks.items() if not v]
        print(f"[{flag}] case {r.case_id:>2}: {r.prompt[:60]:<60} {r.detail}"
              + (f"  FAILED: {bad}" if bad else ""))
    print(f"\n{sum(r.ok for r in results)}/{len(results)} passed ({score:.0%})")
    raise SystemExit(0 if score == 1.0 else 1)


if __name__ == "__main__":
    main()
