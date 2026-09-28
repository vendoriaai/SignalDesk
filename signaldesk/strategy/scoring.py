"""Signal scoring presets (workflows.md Phase 7) + the risk-unit policy.

Versioned so the community can fork and compare strategies (TAD D3).
`trend-momentum-v1` weights: trend 40, momentum 25, RSI regime 15,
volume 10, sentiment adjustment 10. LONG-only; RSI>=75 excluded (R3);
>=80 Fear & Greed caps everything at HOLD.

Risk policy (workflows.md Phase 7/7.5): a stop may not be tighter than
`MIN_RISK_ATR_MULT x ATR(14, daily)` and must leave at least
`MIN_RISK_COST_MULT x round-trip cost` as the risk unit. Below that floor the
cost of trading consumes the R multiple and the 2R/3R targets stop paying for
themselves, so the stop is widened back to the floor and the plan is flagged
(`floored=True`) for disclosure.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

PRESET_NAME = "trend-momentum-v1"

SCORE_THRESHOLD = 60.0
RSI_OVEREXTENDED = 75.0
FNG_EXTREME_GREED = 80.0

# --- trend-momentum-v1 weights (named so the ledger can fingerprint them) -----
TREND_LEG = 13.3333          # close>SMA20, close>SMA50, EMA9>EMA21 (3 x 13.3333)
MOMENTUM_BASE = 15.0         # MACD histogram > 0
MOMENTUM_RISING = 10.0       # MACD histogram rising vs prior bar
RSI_FULL = 15.0              # 50 <= RSI < 70
RSI_HALF = 7.5               # 45-50 or 70-75 (borderline)
VOLUME_WEIGHT = 10.0         # volume >= 30d average
SENTIMENT_GREED = 10.0       # Fear & Greed >= 55
SENTIMENT_NEUTRAL = 5.0      # Fear & Greed 45-55

# --- risk-unit policy -------------------------------------------------------
MIN_RISK_ATR_MULT = 0.75     # R floor in ATR(14, decision timeframe) units
MIN_RISK_COST_MULT = 15.0    # R floor in round-trip-cost units (~0.067R of cost)

# --- regime gates (roadmap item 32, trial T2) -------------------------------
# Declared thresholds, cited in reports. A long-only engine must refuse longs
# when the regime or the candidate's own extension makes the chase the trade.
REGIME_VOL_EXTREME_ANN_PCT = 150.0   # annualized vol above this = mania/panic
REGIME_MAX_7D_GAIN_PCT = 50.0        # 7d return above this = parabolic
REGIME_MAX_SMA20_DIST_PCT = 25.0     # price this far above SMA20 = extended


@dataclass
class SymbolFeatures:
    close: float
    rsi14: float
    atr14: float
    swing_low_20: float
    above_sma20: bool
    above_sma50: bool
    ema9_above_ema21: bool
    macd_hist: float
    macd_hist_prev: float
    volume: float
    volume_avg30: float


@dataclass
class MacroInputs:
    """Snapshot of the USD macro backdrop (WF-3 Phase 5)."""
    dxy_chg_30d_pct: float | None = None
    us10y_chg_30d_bp: float | None = None


# scoring constants
FX_PRESET_NAME = "fx-momentum-v1"
FX_MACRO_WEIGHT = 20.0  # workflows.md WF-3: macro weighted at 20%


@dataclass
class ScoreBreakdown:
    total: float
    trend: float
    momentum: float
    rsi_regime: float
    volume: float
    sentiment: float
    macro: float = 0.0
    reasons: list[str] = field(default_factory=list)
    overextended: bool = False  # RSI >= 75: never a LONG (rule R3)
    hold_capped: bool = False   # extreme greed regime


def score_symbol(f: SymbolFeatures, fear_greed: float | None) -> ScoreBreakdown:
    trend = 0.0
    reasons: list[str] = []
    if f.above_sma20:
        trend += TREND_LEG
        reasons.append("above SMA20")
    if f.above_sma50:
        trend += TREND_LEG
        reasons.append("above SMA50")
    if f.ema9_above_ema21:
        trend += TREND_LEG
        reasons.append("EMA9 > EMA21")

    momentum = 0.0
    if f.macd_hist > 0:
        momentum += MOMENTUM_BASE
        reasons.append("MACD histogram positive")
    if f.macd_hist > f.macd_hist_prev:
        momentum += MOMENTUM_RISING
        reasons.append("MACD histogram rising")

    return _assemble(f, trend, momentum, fear_greed, reasons, volume_weight=VOLUME_WEIGHT)


# --- fx-momentum-v1 (WF-3) ----------------------------------------------------
# Spot FX has no centralized volume and no crypto sentiment index, so those
# weights re-distribute: trend 40 -> kept, momentum 25, RSI 15, macro 20.

def score_fx_symbol(f: SymbolFeatures, macro: MacroInputs, usd_leg: str) -> ScoreBreakdown:
    trend = 0.0
    reasons: list[str] = []
    if f.above_sma20:
        trend += TREND_LEG
        reasons.append("above SMA20")
    if f.above_sma50:
        trend += TREND_LEG
        reasons.append("above SMA50")
    if f.ema9_above_ema21:
        trend += TREND_LEG
        reasons.append("EMA9 > EMA21")

    momentum = 0.0
    if f.macd_hist > 0:
        momentum += MOMENTUM_BASE
        reasons.append("MACD histogram positive")
    if f.macd_hist > f.macd_hist_prev:
        momentum += MOMENTUM_RISING
        reasons.append("MACD histogram rising")

    macro_score = 0.0
    if macro.dxy_chg_30d_pct is not None:
        # LONG signal logic: DXY up favors USD-base longs (USDJPY...), hurts
        # USD-quote longs (EURUSD, XAUUSD).
        favorable = macro.dxy_chg_30d_pct > 0 if usd_leg == "base" else macro.dxy_chg_30d_pct < 0
        if abs(macro.dxy_chg_30d_pct) >= 0.5:  # ignore noise
            if favorable:
                macro_score += 10.0
                reasons.append(f"DXY 30d {macro.dxy_chg_30d_pct:+.1f}% supports USD in {usd_leg} leg")
        else:
            macro_score += 5.0
            reasons.append("DXY flat (neutral)")
    if macro.us10y_chg_30d_bp is not None:
        # Rising yields strengthen USD (same direction rule as DXY);
        # for metals, falling yields are the tailwind.
        if usd_leg == "metal":
            favorable = macro.us10y_chg_30d_bp < 0
        else:
            favorable = macro.us10y_chg_30d_bp > 0 if usd_leg == "base" else macro.us10y_chg_30d_bp < 0
        if abs(macro.us10y_chg_30d_bp) >= 10:
            if favorable:
                macro_score += 10.0
                reasons.append(f"US10Y 30d {macro.us10y_chg_30d_bp:+.0f} bp favorable")
        else:
            macro_score += 5.0
            reasons.append("US10Y flat (neutral)")

    bd = _assemble(f, trend, momentum, None, reasons, volume_weight=0.0)
    bd.macro = round(macro_score, 2)
    bd.total = round(min(100.0, bd.trend + bd.momentum + bd.rsi_regime + bd.macro), 2)
    return bd


def usd_leg(symbol: str) -> str:
    """Where does USD sit in the pair? base (USDJPY), quote (EURUSD), or metal."""
    s = symbol.upper()
    if s.startswith(("XAU", "XAG", "XPT")):
        return "metal"
    return "base" if s.startswith("USD") else "quote"


def _feat_val(feat, key: str) -> float | None:
    v = feat.get(key) if hasattr(feat, "get") else getattr(feat, key, None)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass
class RegimeVerdict:
    """Outcome of the item-32 regime gates for one symbol."""
    reason: str = ""              # "" = the long is permitted
    value: float | None = None    # triggering number, for the derived citation
    column: str = ""              # feature column the number came from
    formula: str = ""             # formula string for the derived citation


def regime_gate(feat) -> RegimeVerdict:
    """Why a symbol fails the regime gates (item 32, trial T2), or a passing
    verdict.

    `feat` is a features row (close, sma200, sma20, vol_ann, ret_7d). NaN or
    missing values mean "cannot judge" and never gate on their own — a short
    200d history is disclosed at report level instead (R4).
    """
    close = _feat_val(feat, "close")
    vol_ann = _feat_val(feat, "vol_ann")
    if vol_ann is not None and vol_ann > REGIME_VOL_EXTREME_ANN_PCT:
        return RegimeVerdict(
            reason=(f"annualized volatility {vol_ann:.0f}% exceeds the "
                    f"{REGIME_VOL_EXTREME_ANN_PCT:.0f}% extremes gate — "
                    "mania/panic regime, longs refused"),
            value=vol_ann, column="vol_ann",
            formula="annualized vol = stdev(20d returns) x sqrt(365) x 100")
    ret_7d = _feat_val(feat, "ret_7d")
    if ret_7d is not None and ret_7d > REGIME_MAX_7D_GAIN_PCT:
        return RegimeVerdict(
            reason=(f"7d return +{ret_7d:.0f}% exceeds the "
                    f"{REGIME_MAX_7D_GAIN_PCT:.0f}% parabolic-extension gate — "
                    "buying it is chasing, not an edge"),
            value=ret_7d, column="ret_7d", formula="(close / close[-7d] - 1) x 100")
    sma20 = _feat_val(feat, "sma20")
    if close is not None and sma20 is not None and sma20 > 0:
        dist = (close / sma20 - 1.0) * 100.0
        if dist > REGIME_MAX_SMA20_DIST_PCT:
            return RegimeVerdict(
                reason=(f"price {dist:.0f}% above SMA20 exceeds the "
                        f"{REGIME_MAX_SMA20_DIST_PCT:.0f}% extension gate — "
                        "extended, wait for the mean"),
                value=dist, column="sma20_dist_pct",
                formula="(close / SMA20 - 1) x 100")
    sma200 = _feat_val(feat, "sma200")
    if close is not None and sma200 is not None and sma200 > 0 and close < sma200:
        return RegimeVerdict(
            reason="price below its 200d SMA — long-only trend gate refuses the long",
            value=close / sma200, column="close_over_sma200",
            formula="close / SMA200 (< 1 refuses the long)")
    return RegimeVerdict()


def market_regime_below_trend(btc_feat) -> bool:
    """True when BTC's own 200d trend is down — caps the whole crypto book
    (long-only engine in a bear regime, item 32)."""
    close = _feat_val(btc_feat, "close")
    sma200 = _feat_val(btc_feat, "sma200")
    return close is not None and sma200 is not None and sma200 > 0 and close < sma200


def _assemble(f: SymbolFeatures, trend: float, momentum: float,
              fear_greed: float | None, reasons: list[str], volume_weight: float) -> ScoreBreakdown:
    r = f.rsi14
    if 50 <= r < 70:
        rsi_score = RSI_FULL
        reasons.append(f"RSI {r:.1f} in 50-70 band")
    elif 45 <= r < 50 or 70 <= r < 75:
        rsi_score = RSI_HALF
        reasons.append(f"RSI {r:.1f} borderline")
    else:
        rsi_score = 0.0

    volume = 0.0
    if volume_weight > 0 and f.volume_avg30 > 0 and f.volume >= f.volume_avg30:
        volume = volume_weight
        reasons.append("volume >= 30d average")

    sentiment = 0.0
    hold_capped = False
    if fear_greed is not None:
        if fear_greed >= FNG_EXTREME_GREED:
            hold_capped = True
        elif fear_greed >= 55:
            sentiment = SENTIMENT_GREED
            reasons.append(f"Fear & Greed {fear_greed:.0f} (greed regime)")
        elif fear_greed >= 45:
            sentiment = SENTIMENT_NEUTRAL
            reasons.append(f"Fear & Greed {fear_greed:.0f} (neutral)")

    overextended = r >= RSI_OVEREXTENDED
    total = round(min(100.0, trend + momentum + rsi_score + volume + sentiment), 2)
    return ScoreBreakdown(
        total=total, trend=round(trend, 2), momentum=momentum, rsi_regime=rsi_score,
        volume=volume, sentiment=sentiment, reasons=reasons,
        overextended=overextended, hold_capped=hold_capped,
    )


@dataclass
class TradePlan:
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr1: str = "2.0R"
    rr2: str = "3.0R"
    floored: bool = False  # risk floor widened the stop (disclosed in the report)


def min_risk_distance(entry: float, atr_daily: float | None,
                      cost_pct: float | None) -> float:
    """Absolute floor for the risk unit (entry - stop), in price terms.

    max(MIN_RISK_ATR_MULT x ATR(daily), MIN_RISK_COST_MULT x round-trip cost).
    Zero when neither input is usable (keeps direct callers/tests unchanged).
    """
    floor = 0.0
    if atr_daily is not None and math.isfinite(atr_daily) and atr_daily > 0:
        floor = max(floor, MIN_RISK_ATR_MULT * atr_daily)
    if cost_pct is not None and math.isfinite(cost_pct) and cost_pct > 0:
        floor = max(floor, MIN_RISK_COST_MULT * cost_pct / 100.0 * entry)
    return floor


def build_trade_plan(entry: float, atr: float, swing_low: float,
                     *, cost_pct: float | None = None) -> TradePlan:
    """stop = closest of (entry - 1.5*ATR, 20d swing low); TP1=+2R, TP2=+3R.

    The stop is then widened to the risk floor (`min_risk_distance`) so the
    daily plan cannot be born with a cost-to-risk ratio that swamps the targets.
    """
    candidates = [entry - 1.5 * atr, swing_low]
    stop = max(candidates, key=lambda s: -abs(entry - s))
    floor = min_risk_distance(entry, atr, cost_pct)
    floored = floor > 0 and (entry - stop) < floor
    if floored:
        stop = entry - floor
    stop = min(stop, entry * (1 - 1e-9))  # never at-or-above entry
    risk = entry - stop
    return TradePlan(entry=entry, stop=stop, tp1=entry + 2 * risk, tp2=entry + 3 * risk,
                     floored=floored)


# --- intraday entry refinement (workflows.md Phase 7.5) -------------------------
# Deterministic LONG rules over lower-timeframe (30m/15m/5m/1m) features; the
# daily ranking above is never touched. Roles: 15m bias, 30m structure stop,
# 5m trigger, 1m micro-timing — each falls back to the next available TF.
#
# The structure stop is ATR-based only. It used to take the 20-bar intraday
# swing low as well, but 20 bars on 30m is ~10 hours — not the ~1 month the same
# lookback means on daily bars — so it produced stops 10-30x tighter than the
# daily plan (R of 0.16% on ETH vs 5.3%), which cost more than 1R to trade. The
# swing low still appears in the cited snapshot; it no longer sets the stop.

PULLBACK_RSI_MID = 68.0      # 15m RSI at/above -> prefer a pullback entry
PULLBACK_RSI_TRIGGER = 72.0  # 5m RSI at/above -> prefer a pullback entry
STOP_ATR_MULT = 1.5          # ATR multiple for structure stops
STOP_FLOOR_MULT = 0.5        # stop never tighter than entry - 0.5*ATR(structure)


@dataclass
class EntryPlan:
    """Refined intraday plan for one signal. mode: market | pullback | wait."""
    mode: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_daily: float
    risk_refined: float
    note: str = ""
    roles: dict = field(default_factory=dict)       # role -> timeframe
    timeframes: dict = field(default_factory=dict)  # tf -> feature snapshot
    floored: bool = False                           # risk floor widened the stop


def _finite(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _bullish_bias(feat: dict) -> bool:
    """close above EMA21 with MACD histogram positive or rising."""
    if not _finite(feat.get("close"), feat.get("ema21"),
                   feat.get("macd_hist"), feat.get("macd_hist_prev")):
        return False
    return feat["close"] > feat["ema21"] and (
        feat["macd_hist"] > 0 or feat["macd_hist"] > feat["macd_hist_prev"]
    )


def _structure_stop(entry: float, atr: float) -> float:
    """Structure stop on the structure timeframe: STOP_ATR_MULT x ATR below entry,
    never tighter than STOP_FLOOR_MULT x ATR. ATR-only by design (see the note
    above the Phase 7.5 constants): an intraday bar-count swing low is not the
    same horizon as a daily one and collapsed the risk unit."""
    return entry - max(STOP_ATR_MULT, STOP_FLOOR_MULT) * atr


def _wait_plan(daily: TradePlan, note: str, roles: dict, snapshot: dict) -> EntryPlan:
    risk = daily.entry - daily.stop
    return EntryPlan("wait", daily.entry, daily.stop, daily.tp1, daily.tp2,
                     risk, risk, note, roles, snapshot)


def refine_entry_plan(daily: TradePlan, ltf: dict[str, dict], *,
                      atr_daily: float | None = None,
                      cost_pct: float | None = None) -> EntryPlan:
    """Refine a daily TradePlan with intraday features (Phase 7.5).

    ltf maps timeframe -> compute_features dict. Rules:
      wait      mid-TF bias bearish (or data unusable) -> keep the daily plan
      pullback  mid-TF bullish but overextended -> enter at the mid EMA21 zone
      market    mid-TF bullish, not overextended -> enter now, tighten the stop
    Stops only tighten (up to the risk floor); TPs stay 2R/3R on the refined
    risk. The daily ranking is never modified.

    `atr_daily` (ATR(14) of the decision timeframe) and `cost_pct` (assumed
    round-trip cost, % of notional) set the risk floor: the refined risk may not
    fall below max(MIN_RISK_ATR_MULT x ATR(daily), MIN_RISK_COST_MULT x cost).
    Passing neither reproduces the pre-floor behaviour (used by unit tests).
    """
    risk_daily = daily.entry - daily.stop
    tfs = {tf: f for tf, f in ltf.items() if isinstance(f, dict) and f}
    if not tfs:
        return _wait_plan(daily, "no intraday data — daily plan unchanged", {}, {})

    roles: dict[str, str] = {}
    mid = next((tf for tf in ("15m", "30m", "1h", "5m", "1m") if tf in tfs), None)
    struct = next((tf for tf in ("30m", "15m", "1h", "5m") if tf in tfs), None)
    trig = next((tf for tf in ("5m", "1m", "15m", "30m") if tf in tfs), None)
    if mid:
        roles["bias"] = mid
    if struct:
        roles["structure"] = struct
    if trig:
        roles["trigger"] = trig
    if "1m" in tfs:
        roles["micro"] = "1m"

    snapshot = {
        tf: {k: round(float(f[k]), 8) for k in
             ("close", "ema9", "ema21", "rsi14", "atr14", "macd_hist", "swing_low_20")
             if _finite(f.get(k))}
        for tf, f in tfs.items()
    }

    m = tfs.get(mid or "", {})
    if not _bullish_bias(m) or not _finite(m.get("atr14")):
        note = f"{mid} bias bearish or incomplete — wait for {mid} close back above EMA21"
        return _wait_plan(daily, note, roles, snapshot)

    trig_rsi = tfs[trig].get("rsi14") if trig else None
    overextended = (
        (_finite(trig_rsi) and trig_rsi >= PULLBACK_RSI_TRIGGER)
        or (_finite(m.get("rsi14")) and m["rsi14"] >= PULLBACK_RSI_MID)
    )

    if overextended and _finite(m.get("ema21"), m.get("atr14")):
        entry = float(m["ema21"])
        stop = _structure_stop(entry, m["atr14"])
        mode = "pullback"
        note = (f"{mid} overextended (RSI {m['rsi14']:.1f}) — "
                f"enter on a pullback to the {mid} EMA21 zone")
    elif overextended:
        return _wait_plan(daily, f"{mid} overextended and LTF structure unusable "
                                 "— daily plan unchanged", roles, snapshot)
    else:
        entry = daily.entry
        s = tfs[struct or mid]
        if not _finite(s.get("atr14")):
            return _wait_plan(daily, "LTF structure unusable — daily plan unchanged",
                              roles, snapshot)
        stop = max(daily.stop, _structure_stop(entry, s["atr14"]))
        mode = "market"
        note = f"momentum aligned on {mid}"
        if trig and trig != mid and _finite(tfs[trig].get("rsi14")):
            note += f"; {trig} trigger RSI {tfs[trig]['rsi14']:.1f}"
        micro = tfs.get("1m")
        if micro and _finite(micro.get("close"), micro.get("ema9")):
            note += ("; 1m above EMA9 (momentum firing)"
                     if micro["close"] > micro["ema9"]
                     else "; 1m below EMA9 (let it settle)")

    floor = min_risk_distance(entry, atr_daily, cost_pct)
    floored = floor > 0 and (entry - stop) < floor
    if floored:
        stop = entry - floor
        note += (f"; stop held at the risk floor max({MIN_RISK_ATR_MULT:g}xATR(1d), "
                 f"{MIN_RISK_COST_MULT:g}x round-trip cost)")
    stop = min(stop, entry * (1 - 1e-9))
    risk = entry - stop
    if risk <= 0 or not math.isfinite(risk):
        return _wait_plan(daily, "refined stop degenerate — daily plan unchanged",
                          roles, snapshot)
    return EntryPlan(mode, entry, stop, entry + 2 * risk, entry + 3 * risk,
                     risk_daily, risk, note, roles, snapshot, floored=floored)


def weights_fingerprint() -> str:
    """Short stable hash of the scoring weights + risk policy.

    Written into the signal ledger so an outcome can always be traced back to
    the exact rule set that produced it (and so A/B comparisons across rule
    changes are possible without trusting file timestamps).
    """
    payload = "|".join(str(x) for x in (
        PRESET_NAME, FX_PRESET_NAME, SCORE_THRESHOLD, RSI_OVEREXTENDED,
        FNG_EXTREME_GREED, TREND_LEG, MOMENTUM_BASE, MOMENTUM_RISING, RSI_FULL,
        RSI_HALF, VOLUME_WEIGHT, SENTIMENT_GREED, SENTIMENT_NEUTRAL, FX_MACRO_WEIGHT,
        MIN_RISK_ATR_MULT, MIN_RISK_COST_MULT, STOP_ATR_MULT, STOP_FLOOR_MULT,
        PULLBACK_RSI_MID, PULLBACK_RSI_TRIGGER,
    ))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
