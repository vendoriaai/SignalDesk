"""Recommended position sizing (roadmap item 30) — advice, never execution.

SignalDesk has no order routing (read-only tools, v1 contract), so sizing
exists only as a per-signal recommendation in the report: how much of an
account to risk on this trade. It never touches the outcome accounting
(rule R7): the ledger and resolver keep measuring the plan's own risk unit
regardless of what anyone stakes.

Model, per the roadmap's evidence (crypto signals run 0.6-0.9 correlated —
ten simultaneous longs are ~1.4 independent bets, not ten):

- Each signal's stop distance (% of entry) is its volatility proxy — the
  plan's own R, already floored against cost by Phase 7/7.5.
- Per-trade account risk scales inversely to that volatility, normalized so
  a median-volatility signal risks `base_risk_pct` (0.75%, the midpoint of
  the 0.5-1% band), clamped to [min_risk_pct, max_risk_pct].
- A cluster cap scales the whole book down when the recommended risks sum
  past `cluster_cap_risk_pct` (default 3% ≈ 3-4 independent bets for one
  market) — correlated positions are one bet repeated, and the cap says so.
- Notional ≈ account-risk% / stop-distance% × 100: risking 0.75% with a 5%
  stop distance is a ~15% notional position. Shown so leverage is visible.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

BASE_RISK_PCT = 0.75           # % of account for a median-volatility signal
MIN_RISK_PCT = 0.5             # roadmap band: 0.5-1% per trade
MAX_RISK_PCT = 1.0
CLUSTER_CAP_RISK_PCT = 3.0     # total recommended risk per market, per report


@dataclass
class Sizing:
    risk_pct_account: float       # % of account risked to the stop
    notional_pct_account: float   # approximate position notional, % of account
    weight: float                 # share of the report's total recommended risk
    capped: bool = False          # cluster cap scaled this signal down


def size_signals(risk_pcts: dict[str, float], *, base_risk_pct: float = BASE_RISK_PCT,
                 min_risk_pct: float = MIN_RISK_PCT, max_risk_pct: float = MAX_RISK_PCT,
                 cluster_cap_risk_pct: float = CLUSTER_CAP_RISK_PCT) -> dict[str, Sizing]:
    """Size one report's signals. `risk_pcts` maps symbol -> stop distance as
    % of entry (the plan's own R); entries with a non-positive distance are
    skipped. Empty/invalid input sizes nothing."""
    valid = {s: v for s, v in risk_pcts.items() if v and v > 0}
    if not valid:
        return {}
    invols = {s: 1.0 / v for s, v in valid.items()}
    med = median(invols.values())
    if med <= 0:
        return {}
    sizes: dict[str, Sizing] = {}
    for sym, inv in invols.items():
        risk = base_risk_pct * (inv / med)
        risk = min(max(risk, min_risk_pct), max_risk_pct)
        sizes[sym] = Sizing(risk_pct_account=round(risk, 4),
                            notional_pct_account=round(risk / valid[sym] * 100.0, 2),
                            weight=0.0)

    total = sum(s.risk_pct_account for s in sizes.values())
    capped = total > cluster_cap_risk_pct
    if capped:
        scale = cluster_cap_risk_pct / total
        for sym, s in sizes.items():
            s.risk_pct_account = round(s.risk_pct_account * scale, 4)
            s.notional_pct_account = round(s.risk_pct_account / valid[sym] * 100.0, 2)
            s.capped = True
        total = sum(s.risk_pct_account for s in sizes.values())
    for sym, s in sizes.items():
        s.weight = round(s.risk_pct_account / total, 4) if total else 0.0
    return sizes
