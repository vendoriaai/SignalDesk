"""Chart rendering (TAD FR-5 chart previews; runs inside the sandbox).

Renders price + SMA20/SMA50 + volume and RSI/MACD panels per symbol into
PNG artifacts, cited as the "visual analysis" evidence the UI streams
during a run (event kind 'chart')."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless renderer

import matplotlib.pyplot as plt
import pandas as pd

from .indicators import atr_wilder, ema, macd, rsi_wilder, sma


def render_symbol_chart(df: pd.DataFrame, symbol: str, out_path: Path,
                        title_suffix: str = "") -> Path:
    """df: date/open/high/low/close/volume ascending. Returns the PNG path."""
    df = df.tail(120).copy()
    df["date"] = pd.to_datetime(df["date"])
    close = df["close"]
    sma20 = sma(close, 20)
    sma50 = sma(close, 50)
    _ = atr_wilder(df["high"], df["low"], close)  # computes consistently w/ scans
    macd_line, macd_sig, macd_hist = macd(close)
    rsi14 = rsi_wilder(close)

    fig, (ax_price, ax_rsi, ax_macd) = plt.subplots(
        3, 1, figsize=(9, 7.2), sharex=True,
        gridspec_kw={"height_ratios": [2.4, 1, 1]},
        constrained_layout=True,
    )
    fig.patch.set_facecolor("#0d1117")
    for ax in (ax_price, ax_rsi, ax_macd):
        ax.set_facecolor("#0d1117")
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        ax.tick_params(colors="#8b949e", labelsize=8)

    ax_price.plot(df["date"], close, color="#e6edf3", lw=1.2, label="close")
    ax_price.plot(df["date"], sma20, color="#58a6ff", lw=0.9, label="SMA20")
    ax_price.plot(df["date"], sma50, color="#d29922", lw=0.9, label="SMA50")
    ax_price.set_title(f"{symbol} — TA chart {title_suffix}".rstrip(), color="#e6edf3", fontsize=11)
    ax_price.legend(loc="upper left", fontsize=7, facecolor="#161b22", labelcolor="#e6edf3")
    ax_price.grid(True, alpha=0.12, color="#8b949e")

    ax_rsi.plot(df["date"], rsi14, color="#bc8cff", lw=1.1)
    ax_rsi.axhline(70, color="#f85149", lw=0.7, ls="--")
    ax_rsi.axhline(50, color="#8b949e", lw=0.5, ls=":")
    ax_rsi.axhline(30, color="#3fb950", lw=0.7, ls="--")
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI(14)", color="#8b949e", fontsize=8)
    ax_rsi.grid(True, alpha=0.12, color="#8b949e")

    colors = ["#3fb950" if h >= 0 else "#f85149" for h in macd_hist]
    ax_macd.bar(df["date"], macd_hist, color=colors, width=0.8)
    ax_macd.plot(df["date"], macd_line, color="#58a6ff", lw=0.9, label="MACD")
    ax_macd.plot(df["date"], macd_sig, color="#d29922", lw=0.9, label="Signal")
    ax_macd.legend(loc="upper left", fontsize=7, facecolor="#161b22", labelcolor="#e6edf3")
    ax_macd.set_ylabel("MACD(12,26,9)", color="#8b949e", fontsize=8)
    ax_macd.grid(True, alpha=0.12, color="#8b949e")

    plt.setp(ax_price.get_xticklabels(), visible=False)
    fig.align_ylabels()
    fig.savefig(out_path, dpi=110, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_entry_chart(df: pd.DataFrame, symbol: str, out_path: Path,
                       title_suffix: str = "",
                       levels: dict[str, float] | None = None) -> Path:
    """Lower-timeframe price + EMA21 with entry/stop/TP level lines (Phase 7.5).

    df: date/open/high/low/close/volume ascending; levels: e.g.
    {"entry": ..., "stop": ..., "TP1": ..., "TP2": ...}. Returns the PNG path.
    """
    df = df.tail(240).copy()
    df["date"] = pd.to_datetime(df["date"])
    close = df["close"]
    ema21 = ema(close, 21)

    fig, ax = plt.subplots(figsize=(9, 4.6), constrained_layout=True)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.tick_params(colors="#8b949e", labelsize=8)
    ax.grid(True, alpha=0.12, color="#8b949e")

    ax.plot(df["date"], close, color="#e6edf3", lw=1.1, label="close")
    ax.plot(df["date"], ema21, color="#58a6ff", lw=0.9, label="EMA21")
    for name, color in (("entry", "#d29922"), ("stop", "#f85149"),
                        ("TP1", "#3fb950"), ("TP2", "#3fb950")):
        value = (levels or {}).get(name)
        if value is None or pd.isna(value):
            continue
        ax.axhline(value, color=color, lw=1.0, ls="--", label=f"{name} {value:.6g}")

    plotted = [v for v in (levels or {}).values() if v is not None and not pd.isna(v)]
    if plotted:
        ymin = min(close.min(), min(plotted))
        ymax = max(close.max(), max(plotted))
        pad = (ymax - ymin) * 0.06 or 1.0
        ax.set_ylim(ymin - pad, ymax + pad)
    ax.set_title(f"{symbol} — entry plan {title_suffix}".rstrip(),
                 color="#e6edf3", fontsize=11)
    ax.legend(loc="upper left", fontsize=7, facecolor="#161b22", labelcolor="#e6edf3")
    fig.savefig(out_path, dpi=110, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return out_path
