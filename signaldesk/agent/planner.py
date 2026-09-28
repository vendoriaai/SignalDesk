"""Planner: prompt -> workflow template + parameters (TAD 3.1, roadmap 2.15).

Two passes:
1. rules — keyword + regex classification, deterministic, no dependencies
2. LLM (BYOK via LiteLLM) — strict JSON refinement when a key is configured;
   any LLM failure or bad parse falls back to the rules answer.
"""
from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, Field


class Workflow(str, Enum):
    MARKET_SCAN = "market_scan"        # WF-1
    DEEP_DIVE = "deep_dive"            # WF-2
    FOREX_SCAN = "forex_scan"          # WF-3 (also metals)
    WATCHLIST_SCAN = "watchlist_scan"  # WF-4


class Plan(BaseModel):
    workflow: Workflow
    market: str = "crypto"          # crypto | forex | metals | equities
    symbol: str | None = None       # deep dive target
    watchlist: str | None = None    # watchlist name (WF-4)
    confidence: float = 1.0
    note: str = ""


_FOREX_HINTS = re.compile(
    r"\b(forex|fx\s|currency|currencies|eurusd|gbpusd|usdjpy|eur/?usd|gbp|eur|jpy)\b", re.I,
)
_METAL_HINTS = re.compile(r"\b(xauusd|gold|xagusd|silver|metals?)\b", re.I)
_CRYPTO_HINTS = re.compile(r"\b(crypto|bitcoin|btc|eth(?:ereum)?|sol(?:ana)?|altcoins?|xrp)\b", re.I)
_EQUITY_HINTS = re.compile(r"\b(stocks?|equities|shares|s&p\s?500|nasdaq|dow)\b", re.I)
_WL_AFTER = re.compile(r"watchlist\s+([\w-]+)", re.I)
_WL_BEFORE = re.compile(r"([\w-]+)\s+watchlist", re.I)
_DEEP_RE = re.compile(
    r"\b(?:deep\s?dive|analy[sz]e|due diligence on|report on|research(?!\s+report)|dd on)\s+"
    r"([A-Za-z][A-Za-z0-9.&-]{0,9})",
    re.I,
)
_EARN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9.&-]{0,9})\s+(?:before|ahead of)\s+earnings\b", re.I)
_EARN2_RE = re.compile(r"\bearnings\s+(?:preview|report|call)\s+(?:for|on)\s+([A-Za-z][A-Za-z0-9.&-]{0,9})", re.I)
_BUY_RE = re.compile(r"\b(?:should i buy|buy|invest in|accumulate)\s+([A-Za-z][A-Za-z0-9.&-]{0,9})\b", re.I)
_TICKER_RE = re.compile(r"\b([A-Z]{1,6})\b")

_METALS_WORDS = {"gold", "xau", "xauusd", "silver", "xag", "xagusd", "metals"}
_COMMON_WORDS = {"THE", "NOW", "BEST", "THIS", "WEEK", "TODAY", "MARKET", "ME", "MY", "A", "FOR",
                 "AND", "OR", "VS", "THIS", "WHAT", "WHICH", "TOP", "NEW", "USD", "US",
                 "STOCKS", "SHARES", "CRYPTO", "FOREX", "WORTH", "LONG", "SHORT"}

_TICKER_STOPWORDS = {"this", "week", "today", "now", "one", "my", "the", "a", "an",
                     "stocks", "shares", "crypto", "forex", "gold", "silver", "worth",
                     "it", "them", "some", "any"}


def _valid_ticker(tok: str) -> bool:
    t = tok.strip(".,?!")
    return len(t) >= 2 and t.lower() not in _TICKER_STOPWORDS


def classify_rules(prompt: str) -> Plan:
    text = prompt.strip()
    low = text.lower()

    # WF-4 watchlist ----------------------------------------------------------
    if "watchlist" in low:
        m = _WL_AFTER.search(low) or _WL_BEFORE.search(low)
        skip = {"my", "the", "a", "this", "that", "your"}
        wl = "default"
        if m:
            cand = (m.group(1) or "").strip("-")
            if cand and cand not in skip:
                wl = cand
        if low.startswith(("watchlist", "rescan")) or " my " in f" {low} ":
            return Plan(workflow=Workflow.WATCHLIST_SCAN, watchlist=wl, market="crypto",
                        note=f"watchlist '{wl}'")
        return Plan(workflow=Workflow.WATCHLIST_SCAN, watchlist=wl, market="crypto",
                    note=f"watchlist '{wl}'")
    if low.startswith("rescan "):
        return Plan(workflow=Workflow.WATCHLIST_SCAN, watchlist="default", market="crypto")

    # WF-2 deep dive on one ticker -------------------------------------------
    for rx, note in ((_DEEP_RE, "explicit deep dive"), (_EARN_RE, "earnings research"),
                     (_EARN2_RE, "earnings preview"), (_BUY_RE, "buy question")):
        m = rx.search(low)
        if m and _valid_ticker(m.group(1)):
            return Plan(workflow=Workflow.DEEP_DIVE, market=_market_for(m.group(1)),
                        symbol=_sym(m.group(1)), note=note)
    tickers = [t for t in _TICKER_RE.findall(text) if t not in _COMMON_WORDS]
    if ("analyze" in low or "analyse" in low or "report on" in low) and len(tickers) == 1:
        return Plan(workflow=Workflow.DEEP_DIVE, market=_market_for(tickers[0]), symbol=_sym(tickers[0]))

    # WF-3 forex / metals ------------------------------------------------------
    trade_verb = re.search(r"\b(scan|outlook|trade|setups?|long|short|move|moving)\b", low)
    if _METAL_HINTS.search(low) and trade_verb:
        return Plan(workflow=Workflow.FOREX_SCAN, market="metals", note="metals scan")
    if _FOREX_HINTS.search(low):
        return Plan(workflow=Workflow.FOREX_SCAN, market="forex", note="forex scan")

    # WF-1 equities (universe adapters land in M2.5) ---------------------------
    if _EQUITY_HINTS.search(low) and ("scan" in low or "best" in low):
        return Plan(workflow=Workflow.MARKET_SCAN, market="equities", note="equities scan")

    # WF-1 crypto (default for scan-ish prompts) -------------------------------
    if _CRYPTO_HINTS.search(low) or "scan" in low or "best" in low:
        market = "metals" if _METAL_HINTS.search(low) else "crypto"
        wf = Workflow.FOREX_SCAN if market == "metals" else Workflow.MARKET_SCAN
        return Plan(workflow=wf, market=market, note="market scan")

    # single bare ticker ("AAPL?", "TSLA analysis")
    if len(tickers) == 1 and ("?" in text or any(v in low for v in ("buy", "price", "worth", "holding"))):
        return Plan(workflow=Workflow.DEEP_DIVE, market=_market_for(tickers[0]), symbol=_sym(tickers[0]),
                    confidence=0.7, note="single ticker")

    # fallback: broad scan on crypto (the canonical WF-1)
    return Plan(workflow=Workflow.MARKET_SCAN, market="crypto", confidence=0.6,
                note="defaulted to crypto market scan")


def _sym(tok: str) -> str:
    s = tok.upper().strip(".,?!")
    return s


def _market_for(symbol: str) -> str:
    s = symbol.upper()
    if s in _METALS_WORDS or s in ("XAUUSD", "XAGUSD"):
        return "metals"
    if len(s) == 6 and s.isalpha() and (s.endswith("USD") or s.startswith("USD") or
                                        s[:3] in ("EUR", "GBP", "AUD", "NZD")):
        return "forex"
    if s in ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "SUI", "TAO", "AAVE", "ENA", "AVAX", "LINK"):
        return "crypto"
    return "equities"


_LLM_SYSTEM = """You are the SignalDesk planner. Map the user prompt to one workflow template
and return ONLY minified JSON, no prose:
{"workflow": one of market_scan|deep_dive|forex_scan|watchlist_scan,
 "market": one of crypto|forex|metals|equities,
 "symbol": uppercase ticker or null,
 "watchlist": watchlist name or null}
Rules: deep_dive requires a single target ticker; watchlist_scan requires a name
(if unclear use "default"); scans of stocks/indices use market=equities with
workflow=market_scan. Metals (gold/silver/XAUUSD) => workflow=forex_scan, market=metals."""


def classify(prompt: str, *, openai_key: str | None = None, anthropic_key: str | None = None,
             openrouter_key: str | None = None, model: str | None = None) -> Plan:
    """LLM-first with rules fallback (and rules-only when no key).

    `model` overrides the per-provider default: pass a full LiteLLM id like
    "openrouter/auto" or set env SIGNALDESK_MODEL / Settings "llm_model".
    """
    creds = None
    if openrouter_key:
        creds = ("openrouter", openrouter_key)
    elif openai_key:
        creds = ("openai", openai_key)
    elif anthropic_key:
        creds = ("anthropic", anthropic_key)
    if creds is not None:
        plan = _classify_llm(prompt, creds, model=model)
        if plan is not None:
            return plan.model_copy(update={"note": (plan.note or "llm")})
    return classify_rules(prompt)


# model ids for the LLM providers we support out of the box
_LLM_MODELS = {"openai": "gpt-4o-mini",
               "anthropic": "claude-haiku-4-5",
               "openrouter": "openrouter/openai/gpt-4o-mini"}


def _classify_llm(prompt: str, creds: tuple[str, str], model: str | None = None) -> Plan | None:
    provider, key = creds
    try:
        import litellm

        model_id = model or _LLM_MODELS.get(provider)
        if model_id is None:
            return None
        resp = litellm.completion(
            model=model_id,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": prompt[:2000]},
            ],
            api_key=key,
            temperature=0,
            max_tokens=120,
            timeout=20,
        )
        text = resp.choices[0].message.content or ""
        payload = json.loads(text[text.index("{"): text.rindex("}") + 1])
        plan = Plan(**payload)
        return plan
    except Exception:
        return None
