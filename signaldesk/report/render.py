"""Render a ScanReport to Markdown (FR-2.6): summary, signals table, risk &
cost table, avoid list, disclosures, numbered citation footnotes, disclaimer."""
from __future__ import annotations

import json

from signaldesk import costs as cost_model

from .schema import ScanReport


def _fmt(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _cite(ids: list[str]) -> str:
    return " ".join(f"[{cid}]" for cid in ids)


def to_markdown(report: ScanReport) -> str:
    lines: list[str] = []
    lines.append(f"# SignalDesk {report.market.title()} Scan — {report.as_of}")
    lines.append("")
    lines.append(f"*Strategy preset: `{report.scoring_preset}` · universe: "
                 f"{', '.join(report.universe)}*")
    if report.generator_model:
        lines.append(f"*Signal generation: AI (Phase 6.5) — selection, direction and "
                     f"score decided by the vision model `{report.generator_model}`; "
                     f"policy gates are advisory warnings (trial "
                     f"ai-signal-generation-v1).*")
    lines.append("")
    lines.append("## Market context")
    lines.append(report.context_summary or "_No market context available._")
    if report.context_claims:
        lines.append("")
        for claim in report.context_claims:
            link = f"[source]({claim.url})" if claim.url else "source unavailable"
            lines.append(f"- ({claim.published or 'undated'}) {claim.claim} — {link}")
    lines.append("")
    if report.sentiment:
        lines.append("## Sentiment")
        for s in report.sentiment:
            lines.append(f"- **{s.index}**: {s.value:g} ({s.regime}) {_cite(s.citations)}")
        lines.append("")
    lines.append("## Ranked signals")
    if report.signals:
        lines.append(
            "| # | Symbol | Dir | Score | Entry | Stop | TP1 | TP2 | R:R | Confluence | Citations |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for i, s in enumerate(report.signals, 1):
            confluence = "; ".join(s.confluence + s.catalysts)
            score_txt = f"{s.score:.0f} (AI)" if s.ai_generated else f"{s.score:.0f}"
            lines.append(
                f"| {i} | {s.symbol} | {s.direction} | {score_txt} | {_fmt(s.entry)} | "
                f"{_fmt(s.stop)} | {_fmt(s.tp1)} | {_fmt(s.tp2)} | {s.rr} | {confluence} | "
                f"{_cite(s.citations)} |"
            )
    else:
        lines.append("_No symbols cleared the score threshold._")
    lines.append("")
    if report.signals:
        lines.append("## Risk units & costs")
        lines.append(
            "*R is the distance from entry to stop. Cost-in-R is the assumed "
            "round-trip cost (fees + spread + slippage) divided by R; the "
            "break-even win rate is the hit rate a 2R target needs just to cover "
            "that cost. Stops are floored so this ratio stays small. Sizing is "
            "advice only — inverse-volatility account risk, cluster-capped per "
            "market; SignalDesk never places orders.*"
        )
        lines.append("")
        lines.append(
            "| Symbol | R (price) | R as % | Account risk | Notional | Weight "
            "| Round-trip cost | Cost in R | Break-even @TP1 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for s in report.signals:
            risk = s.entry - s.stop
            risk_pct = (risk / s.entry * 100.0) if s.entry else 0.0
            be = cost_model.breakeven_win_rate(s.cost_in_r, 2.0)
            if s.sizing:
                acct = f"{s.sizing.risk_pct_account:.2f}%"
                notional = f"~{s.sizing.notional_pct_account:.0f}%"
                weight = f"{s.sizing.weight:.0%}"
            else:
                acct = notional = weight = "—"
            lines.append(f"| {s.symbol} | {_fmt(risk)} | {risk_pct:.2f}% | "
                         f"{acct} | {notional} | {weight} | "
                         f"{s.cost_pct:g}% | {s.cost_in_r:.2f}R | {be:.0%} |")
        lines.append("")
    entry_plans = [s for s in report.signals if s.entry_plan]
    if entry_plans:
        lines.append("## Entry plans (intraday refinement)")
        lines.append(
            "*Lower timeframes (30m/15m/5m/1m) refine the entry, stop and targets; "
            "the daily score is unchanged. A stop may tighten only down to the "
            "risk floor (0.75x ATR(1d) / 15x round-trip cost). Levels carry their "
            "own citations.*"
        )
        lines.append("")
        for s in entry_plans:
            p = s.entry_plan
            if p is None:
                continue
            if p.mode == "wait":
                lines.append(f"- **{s.symbol}** — wait: {p.note} {_cite(p.citations)}")
            else:
                risk_txt = ""
                if p.risk_daily > 0 and p.risk_refined > 0:
                    pct = (1 - p.risk_refined / p.risk_daily) * 100
                    direction = "tighter" if pct >= 0 else "wider"
                    risk_txt = f", risk {p.risk_refined:.6g} ({abs(pct):.0f}% {direction} than daily)"
                floor_txt = " [held at the risk floor]" if p.risk_floored else ""
                lines.append(
                    f"- **{s.symbol}** — {p.mode}: enter {_fmt(p.entry)}, "
                    f"stop {_fmt(p.stop)}{risk_txt}{floor_txt}, TP1 {_fmt(p.tp1)}, "
                    f"TP2 {_fmt(p.tp2)}, cost {p.cost_in_r:.2f}R — {p.note} {_cite(p.citations)}"
                )
        lines.append("")
    if report.avoid:
        lines.append("## Avoid / watch")
        for a in report.avoid:
            lines.append(f"- **{a.symbol}** — {a.reason} {_cite(a.citations)}")
        lines.append("")
    if report.disclosures:
        lines.append("## Disclosures")
        for d in report.disclosures:
            lines.append(f"- {d}")
        lines.append("")
    if report.citations:
        lines.append("## Citations")
        for cite in report.citations.values():
            lines.append(f"- {cite.footnote()}")
        lines.append("")
    lines.append("---")
    lines.append(f"**{report.disclaimer}**")
    lines.append("")
    return "\n".join(lines)


def to_json(report: ScanReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, default=str)


# --- WF-2 deep dive ------------------------------------------------------------


def to_markdown_deepdive(report) -> str:
    """Render a DeepDiveReport as a one-page dossier."""
    lines: list[str] = []
    lines.append(f"# Deep Dive: {report.symbol}" + (f" — {report.name}" if report.name else ""))
    lines.append(f"*{report.as_of}*")
    lines.append("")
    if report.summary:
        lines.append(report.summary)
        lines.append("")

    lines.append("## Technicals")
    t = report.technicals
    if t:
        lines.append(
            f"Close {_fmt(t.get('close', float('nan')))} · RSI(14) {t.get('rsi14', 0):.1f} · "
            f"MACD hist {t.get('macd_hist', 0):.4f} · ATR(14) {_fmt(t.get('atr14', float('nan')))} · "
            f"score {t.get('score', 0):.0f}/100"
        )
        reasons = t.get("reasons") or []
        if reasons:
            lines.append("")
            for reason in reasons:
                lines.append(f"- {reason}")
    lines.append("")
    if report.signal:
        s = report.signal
        lines.append("## Signal")
        lines.append(
            f"**{s.direction} {s.symbol}** score {s.score:.0f} — entry {_fmt(s.entry)}, "
            f"stop {_fmt(s.stop)}, TP1 {_fmt(s.tp1)}, TP2 {_fmt(s.tp2)} ({s.rr})"
        )
        lines.append(
            f"*Risk unit {_fmt(s.entry - s.stop)} · assumed round-trip cost "
            f"{s.cost_pct:g}% = {s.cost_in_r:.2f}R · break-even hit rate at TP1 "
            f"{cost_model.breakeven_win_rate(s.cost_in_r, 2.0):.0%}*"
        )
        lines.append("")
        p = s.entry_plan
        if p is not None:
            if p.mode == "wait":
                lines.append(f"Entry plan: **wait** — {p.note} {_cite(p.citations)}")
            else:
                risk_txt = ""
                if p.risk_daily > 0 and p.risk_refined > 0:
                    pct = (1 - p.risk_refined / p.risk_daily) * 100
                    direction = "tighter" if pct >= 0 else "wider"
                    risk_txt = f" (risk {abs(pct):.0f}% {direction} than daily)"
                floor_txt = " [held at the risk floor]" if p.risk_floored else ""
                lines.append(
                    f"Entry plan ({p.mode}): enter {_fmt(p.entry)}, stop {_fmt(p.stop)}"
                    f"{risk_txt}{floor_txt}, TP1 {_fmt(p.tp1)}, TP2 {_fmt(p.tp2)}, "
                    f"cost {p.cost_in_r:.2f}R — {p.note} {_cite(p.citations)}"
                )
            lines.append("")

    f = report.fundamentals
    lines.append("## Fundamentals")
    if f.name:
        rows = [
            ("Sector / industry", f"{f.sector} / {f.industry}" if f.sector else ""),
            ("Market cap", f"{f.market_cap:,.0f}" if f.market_cap else None),
            ("PE (ttm / fwd)", f"{f.trailing_pe} / {f.forward_pe}" if f.trailing_pe else None),
            ("Price / book", f"{f.price_to_book}" if f.price_to_book else None),
            ("Profit margin", f"{f.profit_margins:.1%}" if f.profit_margins is not None else None),
            ("ROE", f"{f.roe:.1%}" if f.roe is not None else None),
            ("Revenue growth", f"{f.revenue_growth:.1%}" if f.revenue_growth is not None else None),
            ("Consensus", f"{f.recommendation} (target {_fmt(f.target_price or float('nan'))})" if f.recommendation else None),
        ]
        for label, value in rows:
            if value:
                lines.append(f"- **{label}:** {value}")
    else:
        lines.append("_Fundamentals unavailable._")
    lines.append("")

    if report.annuals:
        lines.append("## Annuals (SEC EDGAR)")
        lines.append("| FY | Revenue | Net income |")
        lines.append("|---|---|---|")
        for a in report.annuals:
            rev = f"{a.revenue:,.0f}" if a.revenue is not None else "—"
            ni = f"{a.net_income:,.0f}" if a.net_income is not None else "—"
            lines.append(f"| {a.fy} | {rev} | {ni} |")
        lines.append("")

    if report.earnings_next or report.earnings_history:
        lines.append("## Earnings")
        if report.earnings_next:
            lines.append(f"Next earnings date: **{report.earnings_next}**")
        for e in report.earnings_history[-4:]:
            surprise = f" ({e.surprise_pct:+.1f}%)" if e.surprise_pct is not None else ""
            lines.append(f"- {e.date}: eps est {e.eps_estimate} actual {e.eps_actual}{surprise}")
        lines.append("")

    for heading, claims in (("Bull case", report.bull_case), ("Bear case", report.bear_case)):
        if claims:
            lines.append(f"## {heading}")
            for c in claims:
                link = f" ([source]({c.url}))" if c.url else ""
                lines.append(f"- {c.claim}{link}")
            lines.append("")
    if report.catalysts:
        lines.append("## Catalysts")
        for c in report.catalysts:
            link = f" ([source]({c.url}))" if c.url else ""
            lines.append(f"- ({c.published or 'undated'}) {c.claim}{link}")
        lines.append("")

    if report.disclosures:
        lines.append("## Disclosures")
        for d in report.disclosures:
            lines.append(f"- {d}")
        lines.append("")
    if report.citations:
        lines.append("## Citations")
        for cite in report.citations.values():
            lines.append(f"- {cite.footnote()}")
        lines.append("")
    lines.append("---")
    lines.append(f"**{report.disclaimer}**")
    return "\n".join(lines)
