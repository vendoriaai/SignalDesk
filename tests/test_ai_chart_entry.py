"""Phase 7.6 AI chart read: reconcile geometry, gate-keeping, scan wiring."""
import json
import re
from pathlib import Path

import pytest

from signaldesk.agent import vision
from signaldesk.agent.events import EventBus, EventKind
from signaldesk.citations.registry import CitationRegistry
from signaldesk.ledger import actionable_levels
from signaldesk.report.schema import Signal
from signaldesk.strategy import scoring
from signaldesk.tools import demo
from signaldesk.workflows.entry_refine import (
    apply_ai_chart_reads,
    fetch_ltf_charts,
    maybe_ai_chart_reads,
)
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


def _png(run_dir: Path, name: str) -> str:
    out = run_dir / "sandbox" / "output" / "charts" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_PNG)
    return str(Path("sandbox") / "output" / "charts" / name).replace("\\", "/")


def _signal(sym="BTCUSD", direction="LONG", entry=100.0, stop=95.0):
    return Signal(symbol=sym, direction=direction, score=70.0,
                  entry=entry, stop=stop, tp1=entry + (entry - stop) * 2,
                  tp2=entry + (entry - stop) * 3,
                  citations=[], confluence=[], catalysts=[])


def _ladder(charts, sym, run_dir, tfs=("1d", "1h", "15m")):
    for tf in tfs:
        charts.setdefault(sym, {})[tf] = _png(run_dir, f"{sym}-{tf}.png")


# --- reconcile_ai_entry: geometry -------------------------------------------

def test_reconcile_long_uses_ai_entry_and_stop():
    daily = scoring.TradePlan(entry=100.0, stop=95.0, tp1=110.0, tp2=115.0)
    plan = scoring.reconcile_ai_entry(daily, direction="LONG", ai_entry=100.5,
                                      ai_stop=98.5, atr_daily=2.0, cost_pct=0.1)
    assert plan is not None
    assert plan.entry == 100.5
    assert plan.stop == 98.5          # dist 2.0 above the floor 1.5 -> kept
    assert plan.tp1 == plan.entry + 2 * (plan.entry - plan.stop)
    assert plan.tp2 == plan.entry + 3 * (plan.entry - plan.stop)
    assert not plan.floored


def test_reconcile_floor_binds_and_is_disclosed():
    daily = scoring.TradePlan(entry=100.0, stop=95.0, tp1=110.0, tp2=115.0)
    plan = scoring.reconcile_ai_entry(daily, direction="LONG", ai_entry=100.0,
                                      ai_stop=99.5, atr_daily=2.0, cost_pct=0.1)
    assert plan is not None
    assert plan.floored
    assert plan.stop == plan.entry - 1.5    # 0.75 x ATR(1d)
    assert "risk floor" in plan.note


def test_reconcile_caps_wild_stop():
    daily = scoring.TradePlan(entry=100.0, stop=95.0, tp1=110.0, tp2=115.0)
    plan = scoring.reconcile_ai_entry(daily, direction="LONG", ai_entry=100.0,
                                      ai_stop=90.0, atr_daily=2.0, cost_pct=None)
    assert plan is not None
    assert plan.stop == plan.entry - 6.0    # capped at 3 x ATR(1d)


def test_reconcile_rejects_implausible_entry():
    daily = scoring.TradePlan(entry=100.0, stop=95.0, tp1=110.0, tp2=115.0)
    # 30 away with ATR 2 -> beyond 3 x ATR(1d)
    assert scoring.reconcile_ai_entry(daily, direction="LONG", ai_entry=130.0,
                                      ai_stop=None, atr_daily=2.0, cost_pct=None) is None
    assert scoring.reconcile_ai_entry(daily, direction="LONG", ai_entry=float("nan"),
                                      ai_stop=None, atr_daily=2.0, cost_pct=None) is None


def test_reconcile_short_mirror():
    daily = scoring.TradePlan(entry=100.0, stop=105.0, tp1=90.0, tp2=85.0)
    plan = scoring.reconcile_ai_entry(daily, direction="SHORT", ai_entry=100.5,
                                      ai_stop=102.5, atr_daily=2.0, cost_pct=0.1)
    assert plan is not None
    assert plan.entry == 100.5 and plan.stop == 102.5
    assert plan.tp2 < plan.tp1 < plan.entry < plan.stop


def test_reconcile_without_stop_falls_back_to_draft_risk():
    daily = scoring.TradePlan(entry=100.0, stop=95.0, tp1=110.0, tp2=115.0)
    plan = scoring.reconcile_ai_entry(daily, direction="LONG", ai_entry=100.0,
                                      ai_stop=None, atr_daily=None, cost_pct=None)
    assert plan is not None
    assert plan.stop == 95.0                # dist 0 -> draft risk unit 5.0


# --- apply_ai_chart_reads ----------------------------------------------------

def _registry_with_price(sym):
    registry = CitationRegistry()
    cite = registry.register_direct(100.0, f"{sym} close",
                                    source_tool="quotes", column="price")
    return registry, cite.id


def _fake_read(shift=1.001, stop_frac=0.98):
    def fake(symbol, direction, *, chart_pngs, brief, provider, key, model=None,
             errors=None):
        m = re.search(r"entry ([0-9.eE+-]+)", brief)
        entry = float(m.group(1)) * shift
        return {"reads": {"1d": {"trend": "up", "note": "test read"}},
                "entry": entry, "stop": entry * stop_frac,
                "rationale": "test rationale", "confidence": 0.7}
    return fake


def test_apply_ai_chart_read_updates_signal(tmp_path):
    run_dir = tmp_path
    bus = EventBus()
    charts = {}
    sig = _signal()
    _ladder(charts, sig.symbol, run_dir)
    registry, price_cite = _registry_with_price(sig.symbol)

    monkey_target = vision
    orig = vision.chart_entry_read
    monkey_target.chart_entry_read = _fake_read()
    try:
        applied = apply_ai_chart_reads(
            [sig], charts, run_dir, bus, registry, [],
            provider="openai", key="k", model=None, model_label="gpt-4o-mini",
            price_cite_ids={sig.symbol: price_cite}, atr_daily={sig.symbol: 2.0},
            cost_pcts={sig.symbol: 0.1})
    finally:
        vision.chart_entry_read = orig

    assert applied == 1
    assert sig.entry == pytest.approx(100.1, abs=1e-6)
    assert sig.entry_plan.ai_entry is True
    assert sig.entry_plan.ai_model == "gpt-4o-mini"
    assert sig.entry_plan.timeframes == {}    # no deterministic refinement ran
    for cid in sig.entry_plan.citations:
        assert cid in registry.all(), f"unregistered cite {cid}"
    assert any("AI chart read" in c for c in sig.confluence)
    kinds = [e.kind for e in bus.events]
    assert kinds.count(EventKind.ANALYSIS) >= 2
    assert actionable_levels(sig)[4] == "ai_chart_v1"


def test_apply_ai_chart_read_rejects_implausible(tmp_path):
    run_dir = tmp_path
    bus = EventBus()
    charts = {}
    sig = _signal()
    _ladder(charts, sig.symbol, run_dir)
    registry, price_cite = _registry_with_price(sig.symbol)

    orig = vision.chart_entry_read
    vision.chart_entry_read = _fake_read(shift=3.0)   # entry 3x away -> rejected
    try:
        applied = apply_ai_chart_reads(
            [sig], charts, run_dir, bus, registry, [],
            provider="openai", key="k", model=None, model_label="m",
            price_cite_ids={sig.symbol: price_cite}, atr_daily={sig.symbol: 2.0},
            cost_pcts={sig.symbol: 0.1})
    finally:
        vision.chart_entry_read = orig

    assert applied == 0
    assert sig.entry == 100.0 and sig.entry_plan is None
    assert bus.events[-1].kind == EventKind.WARN


def test_apply_ai_chart_read_degrades_when_vision_fails(tmp_path):
    run_dir = tmp_path
    bus = EventBus()
    charts = {}
    sig = _signal()
    _ladder(charts, sig.symbol, run_dir)
    registry, _ = _registry_with_price(sig.symbol)

    orig = vision.chart_entry_read
    vision.chart_entry_read = lambda *a, **k: None
    try:
        applied = apply_ai_chart_reads(
            [sig], charts, run_dir, bus, registry, [],
            provider="openai", key="k", model=None, model_label="m",
            price_cite_ids={sig.symbol: None})
    finally:
        vision.chart_entry_read = orig

    assert applied == 0
    assert sig.entry == 100.0 and sig.entry_plan is None


# --- maybe_ai_chart_reads: gates --------------------------------------------

def _maybe_args(tmp_path, signals, charts, **kw):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir, signals, charts


def test_maybe_skips_demo_and_missing_creds(tmp_path, monkeypatch):
    run_dir, signals, charts = _maybe_args(tmp_path, [_signal()], {})
    disclosures = []
    assert maybe_ai_chart_reads(signals, charts, run_dir, EventBus(),
                                CitationRegistry(), disclosures,
                                demo_mode=True) == 0
    assert any("Demo scan" in d for d in disclosures)

    disclosures2 = []
    assert maybe_ai_chart_reads(signals, charts, run_dir, EventBus(),
                                CitationRegistry(), disclosures2,
                                demo_mode=False) == 0
    assert any("no LLM key" in d for d in disclosures2)


def test_maybe_respects_settings_toggle(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (tmp_path / "settings.json").write_text(json.dumps({"ai_chart_entry": False}))
    disclosures = []
    applied = maybe_ai_chart_reads([_signal()], {}, run_dir, EventBus(),
                                   CitationRegistry(), disclosures, demo_mode=False)
    assert applied == 0
    assert not any("Demo scan" in d for d in disclosures)


def test_maybe_declares_trial_once(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    charts = {}
    sig = _signal()
    _ladder(charts, sig.symbol, run_dir)
    registry, price_cite = _registry_with_price(sig.symbol)
    monkeypatch.setattr(vision, "resolve_creds", lambda: ("openai", "k", None))
    monkeypatch.setattr(vision, "chart_entry_read", _fake_read())

    assert maybe_ai_chart_reads([sig], charts, run_dir, EventBus(), registry, [],
                                demo_mode=False,
                                price_cite_ids={sig.symbol: price_cite},
                                atr_daily={sig.symbol: 2.0},
                                cost_pcts={sig.symbol: 0.1}) == 1
    assert maybe_ai_chart_reads([sig], charts, run_dir, EventBus(), registry, [],
                                demo_mode=False,
                                price_cite_ids={sig.symbol: price_cite},
                                atr_daily={sig.symbol: 2.0},
                                cost_pcts={sig.symbol: 0.1}) == 1
    from signaldesk import trials as trials_mod

    trial_list = trials_mod.read_trials(run_dir.parent.parent)
    names = [t["name"] for t in trial_list if t["name"] == "ai-chart-entry-v1"]
    assert names == ["ai-chart-entry-v1"], names
    trial = next(t for t in trial_list if t["name"] == "ai-chart-entry-v1")
    assert trial["status"] == "running" and trial["min_signals"] >= 30


# --- fetch_ltf_charts + full scan wiring ------------------------------------

def test_fetch_ltf_charts_renders_every_timeframe(tmp_path):
    from signaldesk.analysis.charts import render_entry_chart  # noqa: F401  (sandbox import check)

    ohlcv = demo.DemoOHLCVTool(tmp_path / "artifacts", "crypto")
    sig = _signal("BTCUSD", direction="SHORT")
    charts = {}
    fetched = fetch_ltf_charts([sig], ["15m", "1m"], ohlcv, tmp_path, EventBus(),
                               [], charts)
    assert set(fetched["BTCUSD"]) == {"15m", "1m"}
    assert set(charts["BTCUSD"]) == {"15m-entry", "1m-entry"}
    for rel in charts["BTCUSD"].values():
        assert (tmp_path / rel).is_file()


def test_market_scan_applies_ai_chart_read_end_to_end(tmp_path, monkeypatch):
    class _LiveOHLCV(demo.DemoOHLCVTool):
        """Demo data behind a non-Demo class name: flips demo_mode so the
        Phase 7.6 path runs (credentials are monkeypatched below)."""

    monkeypatch.setattr(vision, "resolve_creds", lambda: ("openai", "k", None))
    monkeypatch.setattr(vision, "chart_entry_read", _fake_read())
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=4),
        ToolSet(
            movers=demo.DemoMoversTool(tmp_path / "artifacts"),
            quotes=demo.DemoQuotesTool(tmp_path / "artifacts"),
            ohlcv=_LiveOHLCV(tmp_path / "artifacts"),
            fear_greed=demo.DemoFearGreedTool(tmp_path / "artifacts"),
            altseason=demo.DemoAltSeasonTool(tmp_path / "artifacts"),
            search=demo.DemoSearchTool(),
        ),
        bus, tmp_path / "runs" / "run1",
    )
    report = run.report
    assert report.signals
    assert report.citation_coverage == 1.0

    entry_tfs = {"30m-entry", "15m-entry", "5m-entry", "1m-entry"}
    for s in report.signals:
        assert s.entry_plan is not None and s.entry_plan.ai_entry is True
        assert set(report.charts[s.symbol]) >= entry_tfs | {"1d", "1h"}
        for tf in report.charts[s.symbol]:
            assert (run.run_dir / report.charts[s.symbol][tf]).is_file()
        if s.direction == "LONG":
            assert s.stop < s.entry < s.tp1 < s.tp2
        else:
            assert s.tp2 < s.tp1 < s.entry < s.stop
        risk = abs(s.entry - s.stop)
        # stored TPs are rounded from unrounded plan values: allow 1e-6 drift
        assert s.tp1 == pytest.approx(
            s.entry + (2 * risk if s.direction == "LONG" else -2 * risk), abs=1e-6)
        assert s.tp2 == pytest.approx(
            s.entry + (3 * risk if s.direction == "LONG" else -3 * risk), abs=1e-6)
        assert any("AI chart read" in c for c in s.confluence)
        # ledger records carry the AI entry mode (R7 attribution)
        from signaldesk import ledger as ledger_mod

        ledger_file = ledger_mod.default_ledger_path(run.run_dir)
        recs = [json.loads(line) for line in ledger_file.read_text().splitlines() if line.strip()]
        rec = next(r for r in recs if r["symbol"] == s.symbol)
        assert rec["entry_mode"] == "ai_chart_v1"

    # the AI read is streamed before the report phase closes (P7.6 before P8)
    phases = [e.phase for e in bus.events]
    assert "P7.6" in phases
    assert phases.index("P7.6") < len(phases) - 1
    analysis = [e for e in bus.events if e.kind == EventKind.ANALYSIS]
    assert analysis and any("entry ->" in e.message for e in analysis)
    trace = (run.run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"kind": "analysis"' in trace or '"kind":"analysis"' in trace

    # trial declared exactly once for this data dir
    from signaldesk import trials as trials_mod

    trial_list = trials_mod.read_trials(tmp_path)
    assert sum(1 for t in trial_list if t["name"] == "ai-chart-entry-v1") == 1


def test_apply_ai_chart_read_brief_carries_market_context(tmp_path):
    """Phase 7.6: the entry read sees the scan's market context (news, regime)
    alongside the chart ladder — the AI decides on the full picture (R4)."""
    run_dir = tmp_path
    bus = EventBus()
    charts = {}
    sig = _signal()
    _ladder(charts, sig.symbol, run_dir)
    registry, price_cite = _registry_with_price(sig.symbol)

    captured: dict[str, str] = {}

    def fake(symbol, direction, *, chart_pngs, brief, provider, key, model=None,
             errors=None):
        captured[symbol] = brief
        return _fake_read()(symbol, direction, chart_pngs=chart_pngs, brief=brief,
                            provider=provider, key=key, model=model, errors=errors)

    orig = vision.chart_entry_read
    vision.chart_entry_read = fake
    try:
        applied = apply_ai_chart_reads(
            [sig], charts, run_dir, bus, registry, [],
            provider="openai", key="k", model=None, model_label="m",
            price_cite_ids={sig.symbol: price_cite},
            context="news: ETF flows pick up; market regime: BTC below its 200d SMA")
    finally:
        vision.chart_entry_read = orig

    assert applied == 1
    assert "--- market context ---" in captured[sig.symbol]
    assert "ETF flows" in captured[sig.symbol]
    assert "BTC below its 200d SMA" in captured[sig.symbol]
