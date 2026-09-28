"""Outcome statistics: expectancy in R, hit rate, profit factor, time-to-TP1.

Everything here is deliberately interval-first: at the sample sizes a single
user generates, a point estimate is noise. The 95% CI on expectancy uses a
block bootstrap over ISO weeks (signals from the same week are one correlated
bet, not N independent ones — crypto pairs run 0.6-0.9 correlated), and the hit
rate uses a Wilson interval, which stays sane below n=200.
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

RESOLVED_STATUSES = ("tp1", "tp2", "sl", "time")


@dataclass
class Metrics:
    n_rows: int = 0
    n_resolved: int = 0
    n_open: int = 0
    n_no_data: int = 0
    censored_pct: float = 0.0        # share resolved by the time barrier
    blocks: int = 0                  # independent ISO weeks in the sample
    expectancy_r: float = 0.0        # mean r_net (declared 50/50 policy)
    expectancy_ci: tuple[float, float] = (0.0, 0.0)
    expectancy_all_in_r: float = 0.0
    expectancy_runner_r: float = 0.0
    hit_rate: float = 0.0            # TP1 or TP2 reached
    hit_ci: tuple[float, float] = (0.0, 0.0)
    profit_factor: float = 0.0
    median_bars_to_tp1: float = 0.0
    mean_mfe_r: float = 0.0
    mean_mae_r: float = 0.0
    mean_cost_in_r: float = 0.0
    by_mode: dict[str, dict] = field(default_factory=dict)
    by_market: dict[str, dict] = field(default_factory=dict)
    by_symbol: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (safe for small n)."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def week_key(row: dict) -> str:
    """ISO week of the run that emitted the signal (run ids start YYYYMMDDT...)."""
    run_id = str(row.get("signal_id", "")).split(":", 1)[0]
    stamp = run_id.split("T", 1)[0]
    if len(stamp) >= 8 and stamp[:8].isdigit():
        return f"{stamp[:4]}-W{stamp[4:6]}{stamp[6:8]}"
    return "unknown"


def block_bootstrap_ci(rows: list[dict], *, field_name: str = "r_net",
                       n_boot: int = 2000, seed: int = 7) -> tuple[float, float]:
    """95% CI for the mean, resampling whole ISO weeks (not individual trades)."""
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(field_name)
        if value is None:
            continue
        groups[week_key(row)].append(float(value))
    if not groups:
        return (0.0, 0.0)
    keys = sorted(groups)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        sample: list[float] = []
        for _ in keys:
            sample.extend(groups[rng.choice(keys)])
        if sample:
            means.append(sum(sample) / len(sample))
    if not means:
        return (0.0, 0.0)
    means.sort()
    lo = means[max(0, int(0.025 * len(means)))]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    return (round(lo, 3), round(hi, 3))


def _slice_stats(rows: list[dict]) -> dict:
    resolved = [r for r in rows if r.get("status") in RESOLVED_STATUSES]
    if not resolved:
        return {"n": 0}
    r_net = [float(r.get("r_net", 0.0)) for r in resolved]
    hits = [r for r in resolved if r.get("status") in ("tp1", "tp2")]
    return {
        "n": len(resolved),
        "expectancy_r": round(statistics.fmean(r_net), 3),
        "hit_rate": round(len(hits) / len(resolved), 3),
    }


def _group_stats(rows: list[dict], key: str, default: str = "") -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for name in sorted({str(r.get(key, default) or default) for r in rows}):
        stats = _slice_stats([r for r in rows if str(r.get(key, default) or default) == name])
        if stats.get("n"):
            groups[name] = stats
    return groups


def summarize(rows: list[dict]) -> Metrics:
    m = Metrics(n_rows=len(rows))
    resolved = [r for r in rows if r.get("status") in RESOLVED_STATUSES]
    m.n_resolved = len(resolved)
    m.n_open = sum(1 for r in rows if r.get("status") == "open")
    m.n_no_data = sum(1 for r in rows if r.get("status") == "no_data")
    if not resolved:
        m.notes.append("no resolved signals yet — run more scans or widen --horizon")
        return m

    r_net = [float(r.get("r_net", 0.0)) for r in resolved]
    hits = [r for r in resolved if r.get("status") in ("tp1", "tp2")]
    m.censored_pct = round(sum(1 for r in resolved if r.get("status") == "time") / len(resolved), 3)
    m.blocks = len({week_key(r) for r in resolved})
    m.expectancy_r = round(statistics.fmean(r_net), 3)
    m.expectancy_ci = block_bootstrap_ci(resolved)
    m.expectancy_all_in_r = round(statistics.fmean(float(r.get("r_all_in", 0.0)) for r in resolved), 3)
    m.expectancy_runner_r = round(statistics.fmean(float(r.get("r_runner", 0.0)) for r in resolved), 3)
    m.hit_rate = round(len(hits) / len(resolved), 3)
    m.hit_ci = tuple(round(x, 3) for x in wilson_interval(len(hits), len(resolved)))
    wins = sum(x for x in r_net if x > 0)
    losses = -sum(x for x in r_net if x < 0)
    m.profit_factor = round(wins / losses, 3) if losses > 0 else float("inf") if wins > 0 else 0.0
    bars_to_tp1 = [int(r["bars_to_tp1"]) for r in hits if r.get("bars_to_tp1")]
    m.median_bars_to_tp1 = round(statistics.median(bars_to_tp1), 1) if bars_to_tp1 else 0.0
    m.mean_mfe_r = round(statistics.fmean(float(r.get("mfe_r", 0.0)) for r in resolved), 3)
    m.mean_mae_r = round(statistics.fmean(float(r.get("mae_r", 0.0)) for r in resolved), 3)
    m.mean_cost_in_r = round(statistics.fmean(float(r.get("cost_in_r", 0.0)) for r in resolved), 3)
    if m.blocks < 4:
        m.notes.append(f"only {m.blocks} independent week(s): the confidence interval is not yet informative")
    if m.n_resolved < 100:
        m.notes.append(f"n={m.n_resolved} resolved (<100): the 95% CI on expectancy is wider than +/-0.3R")
    if m.censored_pct >= 0.8:
        m.notes.append(f"{m.censored_pct:.0%} of signals were still open at the time barrier: "
                       "these are mark-to-market R's, not barrier hits — let the horizon mature")
    m.by_mode = _group_stats(resolved, "mode", default="daily")
    m.by_market = _group_stats(resolved, "market")
    m.by_symbol = _group_stats(resolved, "symbol")
    return m


def _fmt_ci(ci: tuple[float, float]) -> str:
    return f"[{ci[0]:+.2f}, {ci[1]:+.2f}]"


def render_text(m: Metrics) -> str:
    """Plain-text summary for the CLI (no promises, intervals first)."""
    lines = []
    lines.append(f"signals: {m.n_rows} recorded · {m.n_resolved} resolved · "
                 f"{m.n_open} still open · {m.n_no_data} without data")
    if not m.n_resolved:
        for note in m.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)
    lines.append(f"blocks:  {m.blocks} independent ISO week(s) · "
                 f"time-barrier share {m.censored_pct:.0%}")
    lines.append("")
    lines.append(f"expectancy (50% TP1 + runner, net of cost): {m.expectancy_r:+.3f}R  "
                 f"95% CI {_fmt_ci(m.expectancy_ci)}")
    lines.append(f"  variants: all-in-at-first-target {m.expectancy_all_in_r:+.3f}R · "
                 f"runner-only {m.expectancy_runner_r:+.3f}R")
    lines.append(f"hit rate (TP1+): {m.hit_rate:.1%}  95% CI "
                 f"[{m.hit_ci[0]:.1%}, {m.hit_ci[1]:.1%}]  (n={m.n_resolved})")
    bars_txt = f"{m.median_bars_to_tp1:g}" if m.median_bars_to_tp1 else "n/a (no TP1 hits yet)"
    lines.append(f"profit factor: {m.profit_factor:g} · median bars to TP1: "
                 f"{bars_txt} · mean MFE {m.mean_mfe_r:+.2f}R · "
                 f"mean MAE {m.mean_mae_r:+.2f}R")
    lines.append(f"mean cost drag: {m.mean_cost_in_r:.3f}R per trade")
    for label, group in (("mode", m.by_mode), ("market", m.by_market)):
        if len(group) > 1:
            parts = [f"{k}: {v['expectancy_r']:+.2f}R (n={v['n']}, hit {v['hit_rate']:.0%})"
                     for k, v in group.items()]
            lines.append(f"by {label}: " + " · ".join(parts))
    for note in m.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)
