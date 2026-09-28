"""Phase 2 tests: markets, macro scoring, watchlists, planner, deep dive, evals."""
import pytest

from signaldesk.markets import PROFILES, pip_size, vendor_symbol
from signaldesk.strategy import scoring
from signaldesk.strategy.scoring import MacroInputs, SymbolFeatures


# ---------------------------------------------------------------- symbols ---


def test_vendor_symbol_mapping():
    assert vendor_symbol("BTCUSD", "crypto") == "BTC-USD"
    assert vendor_symbol("EURUSD", "forex") == "EURUSD=X"
    assert vendor_symbol("XAUUSD", "metals") == "GC=F"
    assert vendor_symbol("XAGUSD", "metals") == "SI=F"
    assert vendor_symbol("AAPL", "equities") == "AAPL"


def test_profiles_registered():
    assert set(PROFILES) == {"crypto", "forex", "metals", "equities"}
    assert PROFILES["forex"].preset == "fx"
    assert "EURUSD" in PROFILES["forex"].majors
    assert PROFILES["metals"].majors == ["XAUUSD", "XAGUSD"]


def test_pip_size():
    assert pip_size("USDJPY") == 0.01
    assert pip_size("EURUSD") == 0.0001
    assert pip_size("AAPL") != pip_size("AAPL")  # NaN for non-FX


# -------------------------------------------------------- fx scoring -------


def fx_feats(**over):
    base = dict(
        close=1.08, rsi14=60.0, atr14=0.004, swing_low_20=1.07,
        above_sma20=True, above_sma50=True, ema9_above_ema21=True,
        macd_hist=0.001, macd_hist_prev=0.0005, volume=0.0, volume_avg30=0.0,
    )
    base.update(over)
    return SymbolFeatures(**base)


def test_fx_scoring_full_bullish_quote_pair():
    # EURUSD long with DXY down & yields down: macro fully favorable
    macro = MacroInputs(dxy_chg_30d_pct=-2.0, us10y_chg_30d_bp=-25.0)
    s = scoring.score_fx_symbol(fx_feats(), macro, usd_leg="quote")
    assert s.total == 100.0
    assert s.macro == 20.0


def test_fx_scoring_macro_adverse():
    macro = MacroInputs(dxy_chg_30d_pct=2.0, us10y_chg_30d_bp=25.0)  # USD strengthening
    s = scoring.score_fx_symbol(fx_feats(), macro, usd_leg="quote")  # EURUSD long -> adverse
    assert s.macro == 0.0
    assert s.total < 100


def test_fx_scoring_usd_base_pair():
    macro = MacroInputs(dxy_chg_30d_pct=2.0, us10y_chg_30d_bp=25.0)
    s = scoring.score_fx_symbol(fx_feats(), macro, usd_leg="base")  # USDJPY long -> favorable
    assert s.macro == 20.0


def test_fx_scoring_metal_with_falling_yields():
    macro = MacroInputs(dxy_chg_30d_pct=-1.5, us10y_chg_30d_bp=-30.0)
    s = scoring.score_fx_symbol(fx_feats(), macro, usd_leg="metal")
    assert s.macro == 20.0


def test_fx_macro_noise_is_neutral_half():
    macro = MacroInputs(dxy_chg_30d_pct=0.2, us10y_chg_30d_bp=3.0)
    s = scoring.score_fx_symbol(fx_feats(), macro, usd_leg="quote")
    assert s.macro == 10.0  # 5 + 5 neutral halves


def test_usd_leg_classification():
    assert scoring.usd_leg("USDJPY") == "base"
    assert scoring.usd_leg("EURUSD") == "quote"
    assert scoring.usd_leg("XAUUSD") == "metal"
    assert scoring.usd_leg("XAGUSD") == "metal"


# -------------------------------------------------------- watchlists -------


def test_watchlist_roundtrip(tmp_path):
    from signaldesk import watchlists

    wl = watchlists.add_symbols(tmp_path, "growth", ["BTCUSD", "ETHUSD"], market="crypto")
    assert wl.symbols == ["BTCUSD", "ETHUSD"]
    loaded = watchlists.load(tmp_path, "growth")
    assert loaded.market == "crypto"
    wl = watchlists.remove_symbols(tmp_path, "growth", ["BTCUSD"])
    assert wl.symbols == ["ETHUSD"]
    assert watchlists.load(tmp_path, "growth").symbols == ["ETHUSD"]


def test_watchlist_not_found(tmp_path):
    from signaldesk import watchlists

    with pytest.raises(FileNotFoundError):
        watchlists.load(tmp_path, "nope")


# ---------------------------------------------------------- planner --------


def test_planner_rules_matrix():
    from signaldesk.agent.planner import Workflow, classify_rules

    cases = [
        ("scan the crypto market and find the best pair to trade now", Workflow.MARKET_SCAN, "crypto", None),
        ("scan forex", Workflow.FOREX_SCAN, "forex", None),
        ("scan gold and silver", Workflow.FOREX_SCAN, "metals", None),
        ("is USDJPY setting up for a long this week", Workflow.FOREX_SCAN, "forex", None),
        ("deep dive AAPL", Workflow.DEEP_DIVE, "equities", "AAPL"),
        ("deep-dive NVDA before earnings", Workflow.DEEP_DIVE, "equities", "NVDA"),
        ("earnings preview for AAPL", Workflow.DEEP_DIVE, "equities", "AAPL"),
        ("should I buy NVDA", Workflow.DEEP_DIVE, "equities", "NVDA"),
        ("run due diligence on MSFT", Workflow.DEEP_DIVE, "equities", "MSFT"),
        ("scan my watchlist growth", Workflow.WATCHLIST_SCAN, "crypto", None),
    ]
    for prompt, wf, market, symbol in cases:
        plan = classify_rules(prompt)
        assert plan.workflow == wf, f"{prompt!r}: {plan}"
        assert plan.market == market, f"{prompt!r}: {plan}"
        if symbol:
            assert plan.symbol == symbol, f"{plan!r}"


def test_planner_watchlist_name():
    from signaldesk.agent.planner import Workflow, classify_rules

    for prompt in ("scan my watchlist growth", "what changed in my growth watchlist"):
        plan = classify_rules(prompt)
        assert plan.workflow == Workflow.WATCHLIST_SCAN
        assert plan.watchlist == "growth", f"{prompt!r}: {plan}"


def test_planner_default_is_crypto_scan():
    from signaldesk.agent.planner import Workflow, classify_rules

    plan = classify_rules("what looks interesting today")
    assert plan.workflow == Workflow.MARKET_SCAN and plan.market == "crypto"
    assert plan.confidence < 1.0


def test_openrouter_takes_priority():
    from signaldesk.agent import planner as pl

    captured = {}
    old = pl._classify_llm

    def fake(prompt, creds, model=None):
        captured["creds"] = creds
        captured["model"] = model
        return None  # fall back to rules

    pl._classify_llm = fake
    try:
        pl.classify("scan crypto", openai_key="o", anthropic_key="a", openrouter_key="or")
    finally:
        pl._classify_llm = old
    assert captured["creds"] == ("openrouter", "or")
    assert pl._LLM_MODELS["openrouter"].startswith("openrouter/")
