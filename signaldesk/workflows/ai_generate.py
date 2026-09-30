"""Phase 6.5 — AI signal generation (WF-1/3/4).

When enabled (Settings `ai_signal_generation`, not demo, LLM creds present),
the vision LLM — not the deterministic scoring preset — decides which symbols
become signals: one call per symbol over its daily + 1h TA charts and a data
brief (features, sentiment, BTC regime, market-context news). The reply is
strict JSON: direction LONG/SHORT/NONE, a 0-100 conviction score (>= the scan
threshold means "emit"), a one-line rationale, and an optional invalidation
level that seeds the stop (still floored by the R6 risk floor downstream).

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

from signaldesk.agent import vision as vision_mod
from signaldesk.agent.events import EventBus, EventKind
from signaldesk.citations.registry import CitationRegistry

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
                altseason_value: float | None, btc_note: str,
                context_summary: str) -> str:
    """Text snapshot handed to the vision model alongside the charts."""
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
    if context_summary:
        lines.append("news: " + context_summary[:400])
    return "\n".join(lines)


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
                   context_summary: str,
                   changes_24h: dict[str, float | None]) -> dict[str, dict]:
    """One vision call per symbol; returns {symbol: parsed pick} for symbols
    with a usable reply. Failed symbols are disclosed and simply absent (R4);
    the caller falls back to the deterministic engine when nothing survives."""
    picks: dict[str, dict] = {}
    for sym in features_df.index:
        row = features_df.loc[sym]
        ladder = _chart_ladder(sym, charts, run_dir)
        if len(ladder) < 2:
            disclosures.append(f"{sym}: AI signal generation skipped — daily/1h charts missing.")
            continue
        brief = build_brief(sym, row, changes_24h.get(sym), fng_value,
                            altseason_value, btc_note, context_summary)
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
