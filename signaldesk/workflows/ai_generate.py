"""Phase 6.5 — AI signal generation (WF-1/3/4).

When enabled (Settings `ai_signal_generation`, not demo, LLM creds present),
the vision LLM — not the deterministic scoring preset — decides which symbols
become signals: one call per symbol over its daily + 1h TA charts and a
full-context brief. Every call sees everything the pipeline gathered: the
symbol's indicator snapshot, the exact recent daily bars, the deterministic
policy gates for both directions (advisory — the model may override, but they
are never hidden), the round-trip cost economics in R terms at the risk floor,
a relative-strength line for every other scanned symbol, every collected
market-news claim, and the macro/sentiment backdrop. The reply is strict JSON:
direction LONG/SHORT/NONE, a 0-100 conviction score (>= the scan threshold
means "emit"), a one-line rationale, and an optional invalidation level that
seeds the stop (still floored by the R6 risk floor downstream).

Every asserted decision is registered as a direct `llm_vision` citation (R1);
failed calls degrade that symbol with a disclosure (R4); the deterministic
Phase 7 engine stays as the whole-scan fallback. The first real (non-demo)
application pre-registers trial `ai-signal-generation-v1` with criteria frozen
before any AI-era outcome exists (R7) — see `_ensure_generation_trial`.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pandas as pd

from signaldesk import costs as cost_model
from signaldesk.agent import vision as vision_mod
from signaldesk.agent.events import EventBus, EventKind
from signaldesk.citations.registry import CitationRegistry
from signaldesk.strategy import scoring

TRIAL_NAME = "ai-signal-generation-v1"
GENERATOR = "ai_vision_v1"


def ai_weights_hash(model_label: str) -> str:
    """Fingerprint of the AI rule set (model + prompt version) for the ledger,
    so AI-era outcomes are separable from the deterministic era (R7)."""
    payload = f"{GENERATOR}|{model_label}|{vision_mod._GEN_PROMPT_VERSION}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _num(v, digits: int = 4) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(f):
        return "n/a"
    return f"{f:.{digits}f}"


def _dist_pct(close: float, ma) -> str:
    try:
        ma_f = float(ma)
    except (TypeError, ValueError):
        return "n/a"
    if not (math.isfinite(ma_f) and math.isfinite(close) and ma_f > 0):
        return "n/a"
    return f"{(close / ma_f - 1.0) * 100.0:+.1f}%"


def build_brief(symbol: str, row, chg_24h: float | None, fng_value: float | None,
                altseason_value: float | None, *, policy_notes: str, cost_block: str,
                bars_digest: str, universe_table: str, news_block: str,
                macro_note: str, btc_note: str) -> str:
    """Text snapshot handed to the vision model alongside the charts.

    Per-symbol evidence (indicators, advisory policy gates, cost economics,
    recent daily bars) plus the whole-scan context (every scanned symbol,
    market news, macro) — the model decides on everything the pipeline has.
    """
    close = _num(row.get("close"))
    atr = float(row.get("atr14")) if math.isfinite(_f(row.get("atr14"))) else None
    atr_pct = _num((atr / float(row.get("close"))) * 100.0, 2) if atr else "n/a"
    vol = _f(row.get("volume"))
    vol_avg = _f(row.get("volume_avg30"))
    vol_ratio = _num(vol / vol_avg, 2) if vol_avg and math.isfinite(vol_avg) and vol_avg > 0 else "n/a"
    lines = [
        f"close {close} | 24h {_pct(chg_24h)} | 3d {_num(row.get('ret_3d'), 1)}% | "
        f"7d {_num(row.get('ret_7d'), 1)}% | 30d {_num(row.get('ret_30d'), 1)}%",
        f"RSI14 {_num(row.get('rsi14'), 1)} | MACD hist {_num(row.get('macd_hist'), 6)} "
        f"({'rising' if row.get('macd_hist_rising') else 'falling'}) | "
        f"MACD {'above' if row.get('macd_above_signal') else 'below'} signal",
        f"SMA20 {_num(row.get('sma20'))} (price {_dist_pct(_f(row.get('close')), row.get('sma20'))}) | "
        f"SMA50 {_num(row.get('sma50'))} (price {_dist_pct(_f(row.get('close')), row.get('sma50'))}) | "
        f"SMA200 {_num(row.get('sma200'))} (price {_dist_pct(_f(row.get('close')), row.get('sma200'))})",
        f"ATR14 {_num(row.get('atr14'))} ({atr_pct}% of price) | annualized vol {_num(row.get('vol_ann'), 0)}% | "
        f"volume {vol_ratio}x 30d avg",
        f"20-bar range {_num(row.get('swing_low_20'))} .. {_num(row.get('swing_high_20'))}",
        f"sentiment: Fear & Greed {_num(fng_value, 0) if fng_value is not None else 'n/a'} | "
        f"altcoin season {_num(altseason_value, 1) if altseason_value is not None else 'n/a'}",
    ]
    if btc_note:
        lines.append(btc_note)
    if policy_notes:
        lines.append(policy_notes)
    if cost_block:
        lines.append(cost_block)
    if bars_digest:
        lines.append(bars_digest)
    shared: list[str] = []
    if universe_table:
        shared.append("--- all scanned symbols (relative strength) ---\n" + universe_table)
    if news_block:
        shared.append("--- market news (full article text) ---\n" + news_block)
    if macro_note:
        shared.append("--- macro ---\n" + macro_note)
    if shared:
        lines.append("\n".join(shared))
    return "\n".join(lines)


def build_universe_table(features_df: pd.DataFrame,
                         changes_24h: dict[str, float | None]) -> str:
    """One compact line per scanned symbol, so each per-symbol call sees the
    whole market and can rank relative strength across candidates."""
    lines = []
    for sym in features_df.index:
        row = features_df.loc[sym]
        close, sma200 = _f(row.get("close")), _f(row.get("sma200"))
        sma200_state = ("above" if math.isfinite(sma200) and sma200 > 0 and close > sma200
                        else "below" if math.isfinite(sma200) and sma200 > 0 else "n/a")
        sma_state = (f"{'above' if _truthy(row.get('above_sma20')) else 'below'}/"
                     f"{'above' if _truthy(row.get('above_sma50')) else 'below'}/"
                     f"{sma200_state}")
        lines.append(
            f"{sym}: close {_num(row.get('close'))} | 24h {_pct(changes_24h.get(sym))} | "
            f"7d {_num(row.get('ret_7d'), 1)}% | RSI {_num(row.get('rsi14'), 0)} | "
            f"SMA20/50/200 {sma_state} | vol {_num(_ratio(row.get('volume'), row.get('volume_avg30')), 2)}x avg")
    return "\n".join(lines)


def _truthy(v) -> bool:
    return bool(v) if isinstance(v, (bool,)) else str(v).lower() in ("true", "1")


def _ratio(num, den) -> float:
    d = _f(den)
    return _f(num) / d if math.isfinite(d) and d > 0 else float("nan")


def build_news_block(context_claims: list, *, max_chars: int = 4096) -> str:
    """Every market-context claim the scan collected as FULL article text
    where extraction succeeded (the model reads the article, not a digest);
    the snippet remains the fallback (R4)."""
    lines = []
    for i, c in enumerate(context_claims, 1):
        body = (getattr(c, "text", "") or getattr(c, "claim", "") or "").strip()
        text = " ".join(body.split())[:max_chars]
        pub = getattr(c, "published", "") or "undated"
        lines.append(f"{i}. ({pub}) {text}" if text else f"{i}. ({pub}) [unreadable]")
    return "\n".join(lines)


def build_policy_block(row, fng_value: float | None, btc_below: bool,
                       btc_above: bool) -> str:
    """The deterministic policy gates for BOTH directions, as advisory notes.

    The AI may override them (operator choice on this path) but always sees
    them: regime gates, RSI/FNG guards and the BTC book regime."""
    notes: list[str] = []
    long_gate = scoring.regime_gate(row)
    short_gate = scoring.regime_gate_short(row)
    if long_gate.reason:
        notes.append(f"long gate: {long_gate.reason}")
    if short_gate.reason:
        notes.append(f"short gate: {short_gate.reason}")
    rsi = _f(row.get("rsi14"))
    if math.isfinite(rsi) and rsi >= scoring.RSI_OVEREXTENDED:
        notes.append(f"long guard: RSI {rsi:.1f} >= {scoring.RSI_OVEREXTENDED:.0f} (overextended)")
    if math.isfinite(rsi) and rsi <= scoring.SHORT_RSI_OVEREXTENDED:
        notes.append(f"short guard: RSI {rsi:.1f} <= {scoring.SHORT_RSI_OVEREXTENDED:.0f} (falling knife)")
    if fng_value is not None and math.isfinite(fng_value):
        if fng_value >= scoring.FNG_EXTREME_GREED:
            notes.append(f"long guard: Fear & Greed {fng_value:.0f} — extreme greed cap")
        if fng_value <= scoring.FNG_EXTREME_FEAR:
            notes.append(f"short guard: Fear & Greed {fng_value:.0f} — extreme fear cap")
    if btc_below:
        notes.append("market regime: BTC below its 200d SMA — long book would stand down")
    if btc_above:
        notes.append("market regime: BTC above its 200d SMA — short book would stand down")
    if not notes:
        return ""
    return "policy notes (deterministic gates, advisory for you):\n" + "\n".join(notes)


def build_cost_block(market: str, row, cost_pct: float) -> str:
    """Round-trip cost economics in R terms at the deterministic risk floor —
    the R6 awareness the fallback engine has, handed to the model too."""
    close, atr = _f(row.get("close")), _f(row.get("atr14"))
    if not (math.isfinite(close) and close > 0) or cost_pct <= 0:
        return ""
    floor_parts = [scoring.MIN_RISK_COST_MULT * (cost_pct / 100.0) * close]
    if math.isfinite(atr) and atr > 0:
        floor_parts.append(scoring.MIN_RISK_ATR_MULT * atr)
    stop_dist = max(floor_parts)
    risk_pct = stop_dist / close * 100.0
    cost_r = cost_model.cost_in_r(cost_pct, risk_pct)
    be = cost_model.breakeven_win_rate(cost_r, 2.0)
    return (f"cost economics: round-trip {cost_pct:.3g}% of notional; risk-floor stop "
            f"{risk_pct:.2f}% of price; cost = {cost_r:.2f}R; break-even win rate "
            f"for a 2R target = {be * 100:.0f}% — skip trades this cost would swamp")


def build_bars_digest(run_dir: Path, symbol: str, bars: int = 10) -> str:
    """The last N daily OHLCV bars as exact numbers (the charts show the shape;
    this gives the model the precise recent prices)."""
    path = Path(run_dir) / "sandbox" / "input" / f"ohlcv_{symbol}_1d.csv"
    if not path.is_file():
        return ""
    try:
        df = pd.read_csv(path).tail(bars)
        if df.empty or not {"date", "open", "high", "low", "close"} <= set(df.columns):
            return ""
    except Exception:
        return ""
    lines = []
    for r in df.itertuples():
        vol = getattr(r, "volume", 0.0) or 0.0
        if vol >= 1e9:
            vol_s = f"{vol / 1e9:.2f}B"
        elif vol >= 1e6:
            vol_s = f"{vol / 1e6:.2f}M"
        else:
            vol_s = f"{vol:.0f}"
        lines.append(f"{r.date} O {_num(r.open, 6)} H {_num(r.high, 6)} "
                     f"L {_num(r.low, 6)} C {_num(r.close, 6)} V {vol_s}")
    return "recent daily bars:\n" + "\n".join(lines)


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return f


def _pct(v) -> str:
    return f"{v:+.2f}%" if isinstance(v, (int, float)) and math.isfinite(v) else "n/a"


def _chart_ladder(symbol: str, charts: dict[str, dict[str, str]],
                  run_dir: Path) -> list[tuple[str, Path]]:
    """The (1d, 1h) TA charts P4 rendered for the whole universe."""
    ladder = []
    for tf in ("1d", "1h"):
        rel = (charts.get(symbol) or {}).get(tf)
        if not rel:
            continue
        path = run_dir / rel
        if path.exists():
            ladder.append((tf, path))
    return ladder


def generate_picks(features_df: pd.DataFrame, charts: dict[str, dict[str, str]],
                   run_dir: Path, bus: EventBus, disclosures: list[str], *,
                   provider: str, key: str, model: str | None,
                   model_label: str, fng_value: float | None,
                   altseason_value: float | None, btc_note: str,
                   market: str, btc_below: bool, btc_above: bool,
                   news_block: str, macro_note: str,
                   changes_24h: dict[str, float | None]) -> dict[str, dict]:
    """One vision call per symbol; returns {symbol: parsed pick} for symbols
    with a usable reply. Failed symbols are disclosed and simply absent (R4);
    the caller falls back to the deterministic engine when nothing survives.

    Every call sees the full scan context — its own charts and indicator
    snapshot, the advisory policy gates, the cost economics in R terms, the
    exact recent daily bars, every other scanned symbol's numbers, and the
    market news — so the decision is made on all the data the pipeline has.
    """
    universe_table = build_universe_table(features_df, changes_24h)
    picks: dict[str, dict] = {}
    for sym in features_df.index:
        row = features_df.loc[sym]
        ladder = _chart_ladder(sym, charts, run_dir)
        if len(ladder) < 2:
            disclosures.append(f"{sym}: AI signal generation skipped — daily/1h charts missing.")
            continue
        brief = build_brief(
            sym, row, changes_24h.get(sym), fng_value, altseason_value,
            policy_notes=build_policy_block(row, fng_value, btc_below, btc_above),
            cost_block=build_cost_block(market, row,
                                        cost_model.round_trip_cost_pct(market, sym)),
            bars_digest=build_bars_digest(run_dir, sym),
            universe_table=universe_table, news_block=news_block,
            macro_note=macro_note, btc_note=btc_note)
        errors: list[str] = []
        pick = vision_mod.signal_read(sym, chart_pngs=ladder, brief=brief,
                                      provider=provider, key=key, model=model,
                                      errors=errors)
        if pick is None:
            why = f" ({errors[0]})" if errors else ""
            disclosures.append(f"{sym}: AI signal read failed — symbol skipped (R4){why}.")
            bus.emit("P6.5", EventKind.WARN, f"AI signal read failed for {sym}{why}")
            continue
        picks[sym] = pick
        bus.emit("P6.5", EventKind.ANALYSIS,
                 f"{sym} -> {pick['direction']}, score {pick['score']:.0f}: {pick['rationale']}",
                 symbol=sym, direction=pick["direction"], score=pick["score"],
                 rationale=pick["rationale"], invalidation=pick["invalidation"],
                 model=model_label)
    return picks


def _ensure_generation_trial(data_dir: Path) -> None:
    """Pre-register the AI-generation rule change once, before any of its
    outcomes exist (R7); criteria are frozen at declaration."""
    try:
        from signaldesk import trials as trials_mod

        if any(t.get("name") == TRIAL_NAME for t in trials_mod.read_trials(data_dir)):
            return
        trials_mod.log_trial(
            data_dir, name=TRIAL_NAME,
            hypothesis=("Signals generated by a vision LLM reading each symbol's "
                        "daily+1h charts and a data brief beat the deterministic "
                        "trend-momentum presets on expectancy."),
            change=("Phase 6.5: signal selection, direction and conviction score "
                    "come from the vision model (recorded per report); entry "
                    "stays AI-picked over the 1d->1m ladder; the stop distance "
                    "is floored at max(0.75xATR(1d), 15x cost) and TPs stay "
                    "2R/3R. Policy gates are advisory warnings on this path "
                    "(operator choice), not vetoes. Ledger rows carry "
                    "generator='ai_vision_v1' and an AI weights hash."),
            judging=("Adopt if, over >= min_signals resolved non-demo ledger "
                     "records with generator='ai_vision_v1', the mean R 95% CI "
                     "lower bound is > 0 (signaldesk outcomes). Kill if the "
                     "break-even win rate is unreachable at the observed "
                     "average win/loss, or mean cost_in_r exceeds 0.35."),
            min_signals=30, min_weeks=4,
        )
    except Exception:
        pass
