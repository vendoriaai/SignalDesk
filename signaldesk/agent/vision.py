"""Vision chart read (Phase 7.6): the LLM watches the multi-timeframe chart
ladder and picks the entry point.

One LiteLLM multimodal call per signal: the rendered charts are sent as base64
PNG image parts ordered daily -> 1m, with a text brief (symbol, direction,
draft plan, risk-floor constraint). The reply must be strict JSON; any failure
(API error, bad parse, non-finite entry) returns None and the caller falls
back to the deterministic plan with a disclosure (R4).

The vision read chooses only the entry level. The caller folds it through
`scoring.reconcile_ai_entry`, which clamps the stop distance into the risk
floor — no AI-chosen plan can be born with a cost-to-risk ratio that swamps
the R multiple (R6), and ledger records carry entry_mode="ai_chart_v1" so
outcomes stay attributable to the rule set that produced them (R7).
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path

from .planner import normalize_model_id

_SYSTEM = """You are SignalDesk's chart reader. You are shown the TA charts for ONE
symbol across timeframes from daily down to 1m, in that order (each chart is
labelled with its timeframe; price charts show close + SMA20/SMA50 or EMA21
plus RSI/MACD panels; entry charts add entry/stop/TP level lines).
Decide the best entry point for the stated direction and answer ONLY with
minified JSON, no prose, no markdown fences:
{"reads":{"<tf>":{"trend":"up|down|range","note":"<=12 words"}, ...},
 "entry":<number>,"stop":<number or null>,"rationale":"<=40 words",
 "confidence":<0..1>}
Rules: read what the charts actually show (trend alignment, EMA21/level
pullback zones, structure breaks, momentum shifts across timeframes); the
entry must be a realistic price for the stated direction near the current
market; "stop" is the chart level that invalidates the trade (null lets the
risk floor set the stop distance); never invent levels you cannot see."""

_VISION_TIMEOUT = 90.0
# reasoning models spend tokens thinking before the JSON answer — the full
# multi-chart read has run past 2k reasoning tokens, so leave real headroom
_VISION_MAX_TOKENS = 6000


def _resolve_cfg():
    """Build the runtime Config (env/.env). Separate function so tests can
    stub credential sources without touching os.environ."""
    from ..config import Config

    return Config.from_env()


def resolve_creds() -> tuple[str, str, str | None] | None:
    """(provider, key, model_override) for the vision call, or None.

    Checks env/.env first (Config.from_env, the API/CLI path), then the OS
    keychain (userconfig) — the desktop path pushes keychain secrets to env,
    this covers a bare `python -m signaldesk.cli` too.
    """
    cfg = _resolve_cfg()
    candidates = (("openrouter", "OPENROUTER_API_KEY"),
                  ("openai", "OPENAI_API_KEY"),
                  ("anthropic", "ANTHROPIC_API_KEY"))
    for provider, env_key in candidates:
        key = getattr(cfg, env_key.lower()) or None
        if not key:
            try:
                from .. import userconfig

                key = userconfig.get_secret(env_key)
            except Exception:
                key = None
        if key:
            model = cfg.llm_model
            if model is None:
                try:
                    from .. import userconfig

                    model = userconfig.Settings(cfg.data_dir).get("llm_model") or None
                except Exception:
                    model = None
            return provider, key, model
    return None


def model_id(provider: str, model: str | None) -> str:
    """Full LiteLLM model id (shared normalization with the planner)."""
    return normalize_model_id(provider, model)


def chart_entry_read(symbol: str, direction: str, *, chart_pngs: list[tuple[str, Path]],
                     brief: str, provider: str, key: str, model: str | None = None,
                     errors: list[str] | None = None,
                     ) -> dict | None:
    """Ask a vision-capable LLM to read the chart ladder and pick an entry.

    `chart_pngs` is an ordered [(timeframe, png path)] ladder, daily first.
    `errors`, when given, collects the provider failure text so the caller can
    disclose WHY the read is unavailable (R4) instead of failing silently.
    Returns {"reads": {...}, "entry": float, "stop": float | None,
    "rationale": str, "confidence": float} or None on any failure.
    """
    if not chart_pngs:
        return None
    content: list[dict] = [{"type": "text", "text": (
        f"Symbol: {symbol}. Direction: {direction}. Charts in order, daily -> 1m.\n{brief}"
    )}]
    try:
        for tf, path in chart_pngs:
            b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"Chart: {symbol} {tf}"})
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
    except Exception:
        return None

    try:
        import litellm
        litellm.suppress_debug_info = True  # no "Give Feedback" footer on errors

        resp = litellm.completion(
            model=model_id(provider, model),
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
            api_key=key,
            temperature=0,
            max_tokens=_VISION_MAX_TOKENS,
            timeout=_VISION_TIMEOUT,
        )
        text = resp.choices[0].message.content or ""
    except Exception as exc:
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}"[:300])
        return None
    if not text.strip():
        if errors is not None:
            errors.append("model returned no visible content (reasoning likely "
                          "exhausted the token budget)")
        return None
    read = parse_read(text)
    if read is None and errors is not None:
        errors.append("vision reply was not usable JSON (model may not be vision-capable)")
    return read


def parse_read(text: str) -> dict | None:
    """Strict-JSON extraction + validation; None when unusable (R4 caller)."""
    try:
        payload = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        return None
    entry = payload.get("entry")
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(entry) or entry <= 0:
        return None
    stop = payload.get("stop")
    try:
        stop = float(stop) if stop is not None else None
    except (TypeError, ValueError):
        stop = None
    if stop is not None and not math.isfinite(stop):
        stop = None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    reads = payload.get("reads")
    if not isinstance(reads, dict):
        reads = {}
    clean_reads: dict[str, dict[str, str]] = {}
    for tf, read in reads.items():
        if isinstance(read, dict):
            clean_reads[str(tf)] = {
                "trend": str(read.get("trend", ""))[:20],
                "note": str(read.get("note", ""))[:160],
            }
    return {
        "reads": clean_reads,
        "entry": entry,
        "stop": stop,
        "rationale": str(payload.get("rationale", ""))[:400],
        "confidence": confidence,
    }


# ---- Phase 6.5: AI signal generation ----------------------------------------

_GEN_PROMPT_VERSION = "ai-vision-v3"

_GEN_SYSTEM = """You are SignalDesk's desk analyst deciding trades from charts and data.
You are shown ONE symbol: its daily and 1h TA charts (price + SMA20/50 or EMA21,
RSI and MACD panels), then its full data snapshot — indicator values, the exact
recent daily bars, the deterministic policy gates (advisory for you: you may
override them, they are never hidden from you), the round-trip cost economics
in R terms, every other scanned symbol's key numbers for relative strength,
the market news the scan collected as full article text, and the macro and
sentiment backdrop. Decide the trade for roughly the next 1-3 days and answer
ONLY with minified JSON, no prose, no markdown fences:
{"direction":"LONG"|"SHORT"|"NONE","score":<0-100>,"rationale":"<=40 words",
 "invalidation":<number or null>}
direction = the trade you would take (NONE = no trade worth taking). score =
your conviction in that trade (>= 60 means "emit this signal"; use it to rank
multiple candidates). rationale = the chart/data reason in one line.
invalidation = the price level that proves the trade wrong (your preferred
stop), or null to let the risk floor set the stop distance. The news is
decision evidence, not decoration: read the articles for directional catalysts
(institutional flows, macro prints, regulation, sentiment shifts) and let them
move your direction and score — a strong one-sided catalyst raises conviction,
conflicting news lowers it, and when the news drives or contradicts your call
the rationale must say so. Weigh the technicals and the cost line as well: a
trade whose cost in R swamps its edge is not worth taking. Be decisive: pick
NONE rather than a coin-flip."""


def signal_read(symbol: str, *, chart_pngs: list[tuple[str, Path]], brief: str,
                provider: str, key: str, model: str | None = None,
                errors: list[str] | None = None) -> dict | None:
    """Ask the vision LLM to decide the trade for one symbol (Phase 6.5).

    `chart_pngs` is the ordered [(timeframe, png path)] evidence, daily first.
    Returns {"direction": "LONG"|"SHORT"|"NONE", "score": 0-100,
    "rationale": str, "invalidation": float | None} or None on any failure
    (`errors`, when given, collects the reason for the R4 disclosure).
    """
    if not chart_pngs:
        return None
    content: list[dict] = [{"type": "text", "text": f"Symbol: {symbol}. Charts and data follow.\n{brief}"}]
    try:
        for tf, path in chart_pngs:
            b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"Chart: {symbol} {tf}"})
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
    except Exception as exc:
        if errors is not None:
            errors.append(f"chart payload failed: {exc}"[:200])
        return None

    try:
        import litellm
        litellm.suppress_debug_info = True  # no "Give Feedback" footer on errors

        resp = litellm.completion(
            model=model_id(provider, model),
            messages=[
                {"role": "system", "content": _GEN_SYSTEM},
                {"role": "user", "content": content},
            ],
            api_key=key,
            temperature=0,
            max_tokens=_VISION_MAX_TOKENS,
            timeout=_VISION_TIMEOUT,
        )
        text = resp.choices[0].message.content or ""
    except Exception as exc:
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}"[:300])
        return None
    if not text.strip():
        if errors is not None:
            errors.append("model returned no visible content (reasoning likely "
                          "exhausted the token budget)")
        return None
    parsed = parse_generation(text)
    if parsed is None and errors is not None:
        errors.append("generation reply was not usable JSON")
    return parsed


def parse_generation(text: str) -> dict | None:
    """Strict-JSON extraction + validation for a generation reply; None when
    unusable so the caller degrades that symbol (R4)."""
    try:
        payload = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        return None
    direction = str(payload.get("direction", "")).upper()
    if direction not in ("LONG", "SHORT", "NONE"):
        return None
    try:
        score = float(payload.get("score", 0.0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    score = min(max(score, 0.0), 100.0)
    invalidation = payload.get("invalidation")
    try:
        invalidation = float(invalidation) if invalidation is not None else None
    except (TypeError, ValueError):
        invalidation = None
    if invalidation is not None and not math.isfinite(invalidation):
        invalidation = None
    return {
        "direction": direction,
        "score": round(score, 1),
        "rationale": str(payload.get("rationale", ""))[:400],
        "invalidation": invalidation,
    }
