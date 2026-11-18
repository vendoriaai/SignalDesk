"""Attach close-confirmed SMC and price-action evidence to scan signals.

This module is deliberately advisory: it enriches a signal's explanation but
never changes the existing score, direction, stop, or target. It reads only
OHLCV files already fetched by market_scan.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from signaldesk.strategy.price_action import analyse_price_action
from signaldesk.strategy.smc import analyse_smc

_TF_DURATION = {
    "1d": timedelta(days=1), "1h": timedelta(hours=1),
    "30m": timedelta(minutes=30), "15m": timedelta(minutes=15),
    "5m": timedelta(minutes=5), "1m": timedelta(minutes=1),
}


def _closed_bars(path: Path, timeframe: str, now: datetime) -> tuple[pd.DataFrame, int, bool]:
    """Load bars and remove a still-forming final candle when timestamps exist.

    The data adapter stores Yahoo timestamps as UTC-naive values. We interpret
    them as UTC, matching that adapter's normalization. If a CSV has no usable
    timestamp column, we retain its rows and report that completion could not
    be verified rather than silently dropping an arbitrary candle.
    """
    frame = pd.read_csv(path)
    frame.columns = [str(c).lower() for c in frame.columns]
    stamp_col = next((c for c in ("date", "datetime", "timestamp") if c in frame.columns), None)
    duration = _TF_DURATION.get(timeframe)
    if stamp_col is None:
        return frame, 0, False
    # Daily Yahoo bars represent exchange sessions, not UTC 24h intervals.
    # Keep them intact until exchange-calendar-aware completion logic exists.
    if timeframe == "1d" or duration is None:
        stamps = pd.to_datetime(frame[stamp_col], utc=True, errors="coerce")
        return frame, 0, bool(stamps.notna().any())
    stamps = pd.to_datetime(frame[stamp_col], utc=True, errors="coerce")
    valid = stamps.notna()
    if not valid.any():
        return frame, 0, False
    now_utc = now.astimezone(UTC)
    completed = valid & ((stamps + duration) <= pd.Timestamp(now_utc))
    # Keep invalid timestamp rows for the downstream OHLC validator to reject
    # only if the data itself is malformed; they cannot be safely time-filtered.
    keep = completed | ~valid
    dropped = int((~keep).sum())
    return frame.loc[keep].reset_index(drop=True), dropped, True


def _last(items: list[dict], count: int = 2) -> list[dict]:
    return items[-count:] if items else []


def _describe_timeframe(frame: pd.DataFrame, timeframe: str, side: str) -> list[str]:
    smc = analyse_smc(frame).to_dict()
    pa = analyse_price_action(frame).to_dict()
    out: list[str] = []

    structure = _last(smc["structure"], 2)
    if structure:
        labels = [f"{x['direction']} {x['event']} @ {x['broken_level']:.6g}" for x in structure]
        out.append(f"{timeframe} structure: {', '.join(labels)}")

    sweeps = _last(smc["liquidity_sweeps"], 2)
    if sweeps:
        labels = [f"{x['direction']} sweep @ {x['swept_level']:.6g}" for x in sweeps]
        out.append(f"{timeframe} liquidity: {', '.join(labels)}")

    active_fvgs = [x for x in smc["fair_value_gaps"] if not x["mitigated"]]
    active_obs = [x for x in smc["order_blocks"] if not x["mitigated"]]
    if active_fvgs:
        x = active_fvgs[-1]
        out.append(f"{timeframe} active {x['direction']} FVG {x['lower']:.6g}-{x['upper']:.6g}")
    if active_obs:
        x = active_obs[-1]
        out.append(f"{timeframe} active {x['direction']} order block {x['lower']:.6g}-{x['upper']:.6g}")

    fib = smc.get("fib_retracement")
    if fib:
        keys = [k for k in ("0.382", "0.5", "0.618", "0.786") if k in fib]
        if keys:
            out.append(f"{timeframe} Fibonacci: " + ", ".join(f"{k}={fib[k]:.6g}" for k in keys))

    trend = pa["trend"]
    if trend != "neutral":
        if side in ("LONG", "SHORT"):
            verdict = "aligned" if trend == ("bullish" if side == "LONG" else "bearish") else "countertrend"
        else:
            verdict = "context only"
        out.append(f"{timeframe} price-action trend {trend} ({verdict}, strength {pa['trend_strength']:.2f})")

    recent_patterns = [p for p in pa["patterns"] if p["index"] >= max(0, len(frame) - 3)]
    if recent_patterns:
        labels = [f"{p['name']}({p['direction']})" for p in recent_patterns[-3:]]
        out.append(f"{timeframe} candle confirmation: {', '.join(labels)}")

    levels = pa["levels"]
    if levels:
        close = float(frame.iloc[-1]["close"])
        nearest = sorted(levels, key=lambda x: abs(float(x["price"]) - close))[:2]
        out.append(f"{timeframe} nearest S/R: " + ", ".join(
            f"{x['kind']} {float(x['price']):.6g} ({x['touches']} touches)" for x in nearest))
    return out


def enrich_signals_with_smc_pa(
    signals: list,
    ohlcv_paths: dict[str, list[Path]],
    ltf_csvs: dict[str, dict[str, Path]],
    disclosures: list[str],
    *,
    now: datetime | None = None,
) -> None:
    """Append multi-timeframe SMC/PA evidence to each selected signal."""
    now = now or datetime.now(UTC)
    completion_unknown: set[str] = set()
    failures: list[str] = []
    for signal in signals:
        paths: dict[str, Path] = {}
        for path in ohlcv_paths.get(signal.symbol, []):
            stem = Path(path).stem
            tf = stem.rsplit("_", 1)[-1]
            if tf in ("1d", "1h"):
                paths[tf] = Path(path)
        paths.update({tf: Path(p) for tf, p in ltf_csvs.get(signal.symbol, {}).items()})
        evidence: list[str] = []
        for tf in ("1d", "1h", "30m", "15m", "5m", "1m"):
            path = paths.get(tf)
            if path is None or not path.exists():
                continue
            try:
                bars, dropped, timestamped = _closed_bars(path, tf, now)
                if not timestamped:
                    completion_unknown.add(tf)
                if dropped:
                    # This is expected for the active intraday candle.
                    pass
                if len(bars) < 5:
                    continue
                evidence.extend(_describe_timeframe(bars, tf, signal.direction))
            except Exception as exc:
                failures.append(f"{signal.symbol} {tf}: {exc}")
        if evidence:
            signal.confluence.extend([f"SMC/PA evidence — {line}" for line in evidence])
        else:
            signal.confluence.append("SMC/PA evidence unavailable: insufficient or invalid OHLCV history")

    if completion_unknown:
        disclosures.append(
            "SMC/PA candle completion could not be verified for timeframe(s): "
            + ", ".join(sorted(completion_unknown))
            + "; those rows were retained because no usable timestamp column was present.")
    if failures:
        disclosures.append("SMC/PA analysis partially degraded: " + "; ".join(failures[:8]))
    if signals:
        disclosures.append(
            "SMC/price-action evidence is advisory only and does not alter signal direction, "
            "score, entry, stop or targets. Active FVG/order-block status uses subsequent wick overlap.")


__all__ = ["enrich_signals_with_smc_pa", "append_market_intelligence"]


def append_market_intelligence(
    ohlcv_paths: dict[str, list[Path]],
    ltf_csvs: dict[str, dict[str, Path]],
    disclosures: list[str],
    *,
    now: datetime | None = None,
) -> None:
    """Analyse available market data independently of trade qualification."""
    now = now or datetime.now(UTC)
    analysed = 0
    failures: list[str] = []

    for symbol, csv_paths in sorted(ohlcv_paths.items()):
        paths: dict[str, Path] = {}

        for path in csv_paths:
            path = Path(path)
            timeframe = path.stem.rsplit("_", 1)[-1]
            if timeframe in ("1d", "1h"):
                paths[timeframe] = path

        paths.update({
            tf: Path(path)
            for tf, path in ltf_csvs.get(symbol, {}).items()
        })

        symbol_evidence: list[str] = []

        for timeframe in ("1d", "1h", "30m", "15m", "5m", "1m"):
            path = paths.get(timeframe)
            if path is None or not path.exists():
                continue

            try:
                bars, _, _ = _closed_bars(path, timeframe, now)
                if len(bars) < 5:
                    continue

                # WAIT means trend is context, not confirmation of a trade.
                # _describe_timeframe runs both SMC and Price Action analysis.
                evidence = _describe_timeframe(bars, timeframe, "WAIT")

                if evidence:
                    symbol_evidence.extend(evidence)
                else:
                    symbol_evidence.append(
                        f"{timeframe}: no recent qualifying SMC/PA events detected"
                    )

            except Exception as exc:
                failures.append(f"{symbol} {timeframe}: {exc}")

        if symbol_evidence:
            analysed += 1
            disclosures.append(
                f"MARKET INTELLIGENCE — {symbol}: "
                + "; ".join(symbol_evidence[:8])
            )
        else:
            disclosures.append(
                f"MARKET INTELLIGENCE — {symbol}: insufficient usable "
                "OHLCV history for SMC/PA analysis."
            )

    disclosures.append(
        f"SMC/PA market intelligence analysed {analysed} symbol(s) "
        "independently of trade qualification. This is descriptive evidence, "
        "not a LONG/SHORT recommendation."
    )

    if failures:
        disclosures.append(
            "Market intelligence analysis errors: " + "; ".join(failures[:8])
        )
