"""Phase 6.5 AI signal generation: parser, draft geometry, scan wiring,
gates-as-warnings, fallback paths, ledger attribution, trial pre-registration."""
import json
import re
from pathlib import Path

import pytest

from signaldesk.agent import vision
from signaldesk.agent.events import EventBus, EventKind
from signaldesk.strategy import scoring
from signaldesk.tools import demo
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan


# --- parser ------------------------------------------------------------------

def test_parse_generation_valid_and_normalised():
    read = vision.parse_generation(json.dumps({
        "direction": "short", "score": 88.4, "rationale": "lower highs",
        "invalidation": 2.1}))
    assert read == {"direction": "SHORT", "score": 88.4,
                    "rationale": "lower highs", "invalidation": 2.1}


def test_parse_generation_rejects_and_clamps():
    assert vision.parse_generation('{"direction": "buy", "score": 50}') is None
    assert vision.parse_generation('{"direction": "LONG", "score": "high"}') is None
    assert vision.parse_generation("not json") is None
    read = vision.parse_generation('{"direction": "LONG", "score": 150, "invalidation": "x"}')
    assert read["score"] == 100.0        # clamped
    assert read["invalidation"] is None  # non-numeric -> None


# --- draft plan geometry ------------------------------------------------------

def test_ai_draft_plan_uses_invalidation_and_floor():
    plan = scoring.ai_draft_plan(100.0, 2.0, direction="LONG",
                                 invalidation=98.5, cost_pct=0.1)
    assert plan.entry == 100.0 and plan.stop == 98.5
    assert plan.tp1 == plan.entry + 2 * (plan.entry - plan.stop)
    assert plan.tp2 == plan.entry + 3 * (plan.entry - plan.stop)
    assert not plan.floored


def test_ai_draft_plan_floor_binds_and_caps():
    # stop too tight -> widened to the risk floor
    tight = scoring.ai_draft_plan(100.0, 2.0, direction="LONG",
                                  invalidation=99.5, cost_pct=0.1)
    assert tight.floored and tight.stop == 100.0 - 1.5
    # absurdly far invalidation -> clamped to 3 x ATR
    wild = scoring.ai_draft_plan(100.0, 2.0, direction="LONG",
                                 invalidation=80.0, cost_pct=None)
    assert wild.stop == 100.0 - 6.0
    # no invalidation -> ATR structure stop
    plain = scoring.ai_draft_plan(100.0, 2.0, direction="LONG", cost_pct=None)
    assert plain.stop == 100.0 - 3.0


def test_ai_draft_plan_short_mirror():
    plan = scoring.ai_draft_plan(100.0, 2.0, direction="SHORT",
                                 invalidation=102.0, cost_pct=0.1)
    assert plan.stop == 102.0
    assert plan.tp2 < plan.tp1 < plan.entry < plan.stop


# --- scan wiring --------------------------------------------------------------

class _LiveOHLCV(demo.DemoOHLCVTool):
    """Demo data behind a non-Demo class name: flips demo_mode so the AI path
    is exercised (credentials are monkeypatched in the AI tests)."""


def _tools(tmp_path):
    d = tmp_path / "artifacts"
    return ToolSet(
        movers=demo.DemoMoversTool(d, "crypto"),
        quotes=demo.DemoQuotesTool(d, "crypto"),
        ohlcv=_LiveOHLCV(d, "crypto"),
        search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(d),
        altseason=demo.DemoAltSeasonTool(d),
    )


def _fake_generation(picks_by_symbol):
    """signal_read stub keyed by symbol; unknown symbols -> NONE low score."""

    def fake(symbol, *, chart_pngs, brief, provider, key, model=None, errors=None):
        pick = picks_by_symbol.get(symbol, {"direction": "NONE", "score": 20.0,
                                            "rationale": "no edge", "invalidation": None})
        return dict(pick)

    return fake


def _fake_entry_read():
    def fake(symbol, direction, *, chart_pngs, brief, provider, key, model=None, errors=None):
        m = re.search(r"entry ([0-9.eE+-]+)", brief)
        entry = float(m.group(1))
        return {"reads": {"1d": {"trend": "up", "note": "test"}},
                "entry": entry * 1.001, "stop": entry * 0.98,
                "rationale": "entry read", "confidence": 0.6}
    return fake


def test_scan_ai_generates_signals_and_gates_warn(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "resolve_creds", lambda: ("openai", "k", None))
    # bear demo market: BTC below its 200d SMA. The AI overrides it — a LONG
    # pick must be EMITTED with a policy-gate warning, not refused.
    monkeypatch.setattr(vision, "signal_read", _fake_generation({
        "BTCUSD": {"direction": "SHORT", "score": 88.0, "rationale": "test short",
                   "invalidation": None},
        "ETHUSD": {"direction": "LONG", "score": 75.0, "rationale": "test long override",
                   "invalidation": None},
        "SOLUSD": {"direction": "NONE", "score": 30.0, "rationale": "chop", "invalidation": None},
    }))
    monkeypatch.setattr(vision, "chart_entry_read", _fake_entry_read())
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=6),
        _tools(tmp_path), bus, tmp_path / "runs" / "run1",
    )
    r = run.report
    assert r.generator_model and r.scoring_preset == "ai-vision-v1"
    sigs = {s.symbol: s for s in r.signals}
    assert "BTCUSD" in sigs and sigs["BTCUSD"].direction == "SHORT"
    assert sigs["BTCUSD"].ai_generated is True
    assert sigs["BTCUSD"].score == 88.0
    assert any("AI decision: SHORT score 88/100" in c for c in sigs["BTCUSD"].confluence)
    # gates warn instead of vetoing: the LONG in a bear market still emits
    assert "ETHUSD" in sigs and sigs["ETHUSD"].direction == "LONG"
    assert any("policy gate" in c and "AI proceeded" in c
               for c in sigs["ETHUSD"].confluence)
    # geometry stays R6-clean per direction
    for s in r.signals:
        if s.direction == "LONG":
            assert s.stop < s.entry < s.tp1 < s.tp2
        else:
            assert s.tp2 < s.tp1 < s.entry < s.stop
        assert 0 < s.cost_in_r <= 1 / 15 + 1e-6
    # AI passes land in the avoid list with the rationale
    assert any(a.symbol == "SOLUSD" and "AI passed" in a.reason and "chop" in a.reason
               for a in r.avoid)
    # the generation phase streamed before the report
    phases = [e.phase for e in bus.events]
    assert "P6.5" in phases
    gen_events = [e for e in bus.events
                  if e.phase == "P6.5" and e.kind == EventKind.ANALYSIS]
    assert gen_events and any(e.data.get("direction") == "SHORT" for e in gen_events)
    assert any("AI-generated signals" in d for d in r.disclosures)
    assert r.citation_coverage == 1.0

    # ledger attribution: generator + AI weights hash, separable from the preset
    from signaldesk import ledger as ledger_mod

    ledger_file = ledger_mod.default_ledger_path(tmp_path / "runs" / "run1")
    recs = [json.loads(l) for l in ledger_file.read_text().splitlines() if l.strip()]
    assert recs and all(rec["generator"] == "ai_vision_v1" and rec["generator_model"]
                        for rec in recs)
    assert all(rec["weights_hash"] != scoring.weights_fingerprint() for rec in recs)

    # trial pre-registered exactly once for this data dir
    from signaldesk import trials as trials_mod

    trial_list = trials_mod.read_trials(tmp_path)
    assert sum(1 for t in trial_list if t["name"] == "ai-signal-generation-v1") == 1


def test_scan_falls_back_without_creds(tmp_path):
    # conftest strips LLM credentials: deterministic engine must run + disclose
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=4),
        _tools(tmp_path), bus, tmp_path,
    )
    r = run.report
    assert r.signals, "deterministic demo must still produce signals"
    assert all(not s.ai_generated for s in r.signals)
    assert r.scoring_preset == scoring.PRESET_NAME
    assert any("AI signal generation unavailable" in d for d in r.disclosures)
    assert not any(e.phase == "P6.5" for e in bus.events)


def test_scan_falls_back_when_all_reads_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "resolve_creds", lambda: ("openai", "k", None))
    monkeypatch.setattr(vision, "signal_read", lambda *a, **k: None)
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=4),
        _tools(tmp_path), EventBus(), tmp_path,
    )
    r = run.report
    assert r.signals
    assert all(not s.ai_generated for s in r.signals)
    assert any("returned no usable reads" in d for d in r.disclosures)
    assert r.generator_model == ""


def test_setting_off_uses_deterministic(tmp_path, monkeypatch):
    data_dir = tmp_path
    (data_dir / "settings.json").write_text(json.dumps({"ai_signal_generation": False}))
    monkeypatch.setattr(vision, "resolve_creds", lambda: ("openai", "k", None))
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return None

    monkeypatch.setattr(vision, "signal_read", spy)
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=4),
        _tools(tmp_path), EventBus(), tmp_path / "runs" / "run1",
    )
    assert run.report.signals
    assert called["n"] == 0
    assert all(not s.ai_generated for s in run.report.signals)


def test_demo_scan_never_calls_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "resolve_creds", lambda: ("openai", "k", None))
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return None

    monkeypatch.setattr(vision, "signal_read", spy)
    from signaldesk.agent.events import EventBus as Bus
    from signaldesk.tools import demo as dm

    tools = ToolSet(
        movers=dm.DemoMoversTool(tmp_path / "artifacts", "crypto"),
        quotes=dm.DemoQuotesTool(tmp_path / "artifacts", "crypto"),
        ohlcv=dm.DemoOHLCVTool(tmp_path / "artifacts", "crypto"),
        search=dm.DemoSearchTool(),
        fear_greed=dm.DemoFearGreedTool(tmp_path / "artifacts"),
        altseason=dm.DemoAltSeasonTool(tmp_path / "artifacts"),
    )
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          tools, Bus(), tmp_path)
    assert run.report.signals
    assert called["n"] == 0   # demo (offline): deterministic engine, no LLM
    assert any("AI signal generation unavailable" in d or "deterministic" in d
               for d in run.report.disclosures)


# --- full-context brief: the AI sees everything the pipeline gathered --------


def test_universe_table_lists_every_symbol():
    import pandas as pd

    from signaldesk.workflows.ai_generate import build_universe_table

    df = pd.DataFrame({  # booleans as strings: features.csv round-trip
        "close": [100.0, 2.0], "rsi14": [55.0, 72.0], "ret_7d": [1.0, -3.0],
        "sma200": [90.0, 3.0], "above_sma20": ["True", "False"],
        "above_sma50": ["True", "False"], "volume": [120.0, 50.0],
        "volume_avg30": [100.0, 100.0],
    }, index=["BTCUSD", "XYZUSD"])
    table = build_universe_table(df, {"BTCUSD": 1.2, "XYZUSD": -0.5})
    assert "BTCUSD" in table and "XYZUSD" in table
    assert "SMA20/50/200 above/above/above" in table   # 100 > 90
    assert "SMA20/50/200 below/below/below" in table   # 2 < 3
    assert "+1.20%" in table


def test_news_block_keeps_every_claim():
    from signaldesk.report.schema import NewsClaim
    from signaldesk.workflows.ai_generate import build_news_block

    claims = [NewsClaim(claim="ETF inflows surge", published="2026-10-01"),
              NewsClaim(claim="Funding rates reset lower", published="2026-09-30")]
    block = build_news_block(claims)
    assert "1. (2026-10-01) ETF inflows surge" in block
    assert "2. (2026-09-30) Funding rates reset lower" in block


def test_cost_block_matches_r6_floor_math():
    import pandas as pd

    from signaldesk.costs import breakeven_win_rate, cost_in_r
    from signaldesk.workflows.ai_generate import build_cost_block

    row = pd.Series({"close": 100.0, "atr14": 2.0})
    block = build_cost_block("crypto", row, 0.1)
    # floor stop = max(0.75 x ATR, 15 x cost) = max(1.5, 1.5) = 1.5 -> 1.5% risk
    cost_r = cost_in_r(0.1, 1.5)
    assert "risk-floor stop 1.50% of price" in block
    assert f"cost = {cost_r:.2f}R" in block
    assert f"{breakeven_win_rate(cost_r, 2.0) * 100:.0f}%" in block


def test_policy_block_lists_gates_for_both_sides():
    import pandas as pd

    from signaldesk.workflows.ai_generate import build_policy_block

    row = pd.Series({"rsi14": 80.0, "close": 80.0, "sma200": 90.0,
                     "sma20": 79.0, "vol_ann": 40.0, "ret_7d": 2.0})
    block = build_policy_block(row, fng_value=None, btc_below=True, btc_above=False)
    assert "advisory" in block
    assert "price below its 200d SMA" in block          # regime gate reason
    assert "RSI 80.0 >= 75" in block                    # overextension guard
    assert "BTC below its 200d SMA" in block            # book regime
    # no 200d SMA -> the trend gate cannot judge; a mid RSI leaves no notes
    clean = pd.Series({"rsi14": 50.0, "close": 100.0, "sma200": float("nan"),
                       "sma20": 95.0, "vol_ann": 40.0, "ret_7d": 2.0})
    assert build_policy_block(clean, fng_value=55.0, btc_below=False, btc_above=False) == ""


def test_bars_digest_reads_exact_bars(tmp_path):
    from signaldesk.workflows.ai_generate import build_bars_digest

    d = tmp_path / "sandbox" / "input"
    d.mkdir(parents=True)
    (d / "ohlcv_TEST_1d.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2026-09-30,1.0,1.2,0.9,1.1,1000\n"
        "2026-10-01,1.1,1.3,1.0,1.25,1500\n")
    digest = build_bars_digest(tmp_path, "TEST")
    assert "recent daily bars:" in digest
    assert "2026-10-01 O 1.100000 H 1.300000 L 1.000000 C 1.250000 V 1500" in digest

def test_scan_brief_carries_full_context(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "resolve_creds", lambda: ("openai", "k", None))
    briefs: dict[str, str] = {}

    def fake(symbol, *, chart_pngs, brief, provider, key, model=None, errors=None):
        briefs[symbol] = brief
        return {"direction": "NONE", "score": 10.0, "rationale": "test",
                "invalidation": None}

    monkeypatch.setattr(vision, "signal_read", fake)
    run_market_scan(
        MarketScanRequest(market="crypto", universe_size=6),
        _tools(tmp_path), EventBus(), tmp_path / "runs" / "run1",
    )
    assert briefs, "AI path ran and collected briefs"
    btc = briefs["BTCUSD"]
    assert "--- all scanned symbols" in btc and "ETHUSD" in btc  # whole universe
    assert "recent daily bars:" in btc                            # exact prices
    assert "cost economics:" in btc and "break-even win rate" in btc
    assert "--- market news" in btc and "demo research note" in btc  # all claims
    assert "sentiment: Fear & Greed" in btc                       # sentiment kept
