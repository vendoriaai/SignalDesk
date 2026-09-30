"""End-to-end WF-1 test on the offline demo market (no network)."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from signaldesk.agent.events import EventBus
from signaldesk.tools import demo
from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan


def _demo_tools(artifacts_dir):
    return ToolSet(
        movers=demo.DemoMoversTool(artifacts_dir),
        quotes=demo.DemoQuotesTool(artifacts_dir),
        ohlcv=demo.DemoOHLCVTool(artifacts_dir),
        fear_greed=demo.DemoFearGreedTool(artifacts_dir),
        altseason=demo.DemoAltSeasonTool(artifacts_dir),
        search=demo.DemoSearchTool(),
    )


def test_market_scan_end_to_end(tmp_path):
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=12),
        _demo_tools(tmp_path / "artifacts"),
        bus,
        tmp_path,
    )
    report = run.report

    # golden assertions from roadmap Phase 1 exit test
    assert report.market == "crypto"
    assert len(report.citations) >= 20
    assert report.citation_coverage == 1.0
    assert report.signals, "expected at least one signal in demo market"
    assert len(report.signals) <= 5

    # R3/R5: no signal may be RSI-overextended, and every signal is cited
    for signal in report.signals:
        assert signal.score >= 60
        assert signal.citations
        if signal.direction == "SHORT":
            assert signal.tp2 < signal.tp1 < signal.entry < signal.stop
        else:
            assert signal.stop < signal.entry < signal.tp1 < signal.tp2
        rsi_cite = next(c for c in signal.citations if "RSI" in report.citations[c].label)
        if signal.direction == "SHORT":
            assert report.citations[rsi_cite].value > 25
        else:
            assert report.citations[rsi_cite].value < 75

    # persisted artifacts
    assert (run.run_dir / "report.md").exists()
    assert (run.run_dir / "report.json").exists()
    assert (run.run_dir / "trace.jsonl").exists()
    assert "Not financial advice" in run.report_md
    assert bus.events, "trace must be non-empty"


def test_overextended_symbol_goes_to_avoid(tmp_path):
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_size=12),
        _demo_tools(tmp_path / "artifacts"),
        bus,
        tmp_path,
    )
    # the demo market's parabolic runner must never show up as a signal
    signal_symbols = {s.symbol for s in run.report.signals}
    features = pd.read_csv(tmp_path / "sandbox" / "output" / "features.csv", index_col=0)
    for sym, row in features.iterrows():
        if row["rsi14"] >= 75:
            assert sym not in signal_symbols


def test_unknown_market_rejected(tmp_path):
    import pytest

    bus = EventBus()
    with pytest.raises(ValueError, match="unknown market"):
        run_market_scan(
            MarketScanRequest(market="commodities"),
            _demo_tools(tmp_path / "artifacts"),
            bus,
            tmp_path,
        )


# --- SHORT side: a hard downtrend must emit a SHORT signal ------------------------

import numpy as np

from signaldesk.tools.base import ToolResult


def _bearish_frame(days: int = 380, base: float = 100.0) -> pd.DataFrame:
    """Deterministic gentle downtrend: below all SMAs, MACD fading, RSI mid-band
    (seed 19 verified: RSI ~40, 7d ~-3%, annualized vol ~20%)."""
    rng = np.random.default_rng(19)
    rets = -0.0025 + rng.normal(0.0, 0.009, days)
    close = base * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0045, days)) + 0.001
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = np.full(days, 1.0e6)   # constant: the volume leg always passes
    dates = pd.date_range(end=pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=1),
                          periods=days, freq="D")
    return pd.DataFrame({"date": dates, "open": open_, "high": high, "low": low,
                         "close": close, "volume": volume})


class DemoBearOHLCVTool(demo.DemoOHLCVTool):
    """The demo tool with BEARUSD swapped to the downtrend frame (any timeframe)."""

    def run(self, symbol: str, period: str = "6mo", interval: str = "1d") -> ToolResult:
        if symbol.upper() == "BEARUSD":
            path = self.artifacts_dir / f"ohlcv_{symbol}_{interval}.csv"
            _bearish_frame().to_csv(path, index=False)
            return ToolResult(csv_files=[path],
                              summary=f"{symbol} {interval}: 380 bars",
                              sources=[self._source()])
        return super().run(symbol=symbol, period=period, interval=interval)


class DemoBearQuotesTool(demo.DemoQuotesTool):
    def run(self, symbols: list[str]) -> ToolResult:
        if symbols == ["BEARUSD"]:
            df = _bearish_frame()
            last, prev = df.iloc[-1], df.iloc[-2]
            tail = df.tail(min(360, len(df)))
            out = pd.DataFrame([{
                "symbol": "BEARUSD", "price": round(float(last["close"]), 8),
                "change_24h_pct": round(float((last["close"] / prev["close"] - 1) * 100), 4),
                "day_low": float(last["low"]), "day_high": float(last["high"]),
                "year_low": float(tail["low"].min()), "year_high": float(tail["high"].max()),
                "volume": float(last["volume"]),
            }])
            path = self.artifacts_dir / "quotes_bear.csv"
            out.to_csv(path, index=False)
            return ToolResult(csv_files=[path], summary="quotes for 1 symbol (bear fixture)",
                              sources=[self._source()])
        return super().run(symbols=symbols)


def _bear_tools(artifacts_dir):
    return ToolSet(
        movers=demo.DemoMoversTool(artifacts_dir),
        quotes=DemoBearQuotesTool(artifacts_dir),
        ohlcv=DemoBearOHLCVTool(artifacts_dir),
        fear_greed=demo.DemoFearGreedTool(artifacts_dir),
        altseason=None,
        search=demo.DemoSearchTool(),
    )


def test_downtrend_emits_short_signal(tmp_path):
    bus = EventBus()
    run = run_market_scan(
        MarketScanRequest(market="crypto", universe_override=["BEARUSD"], universe_size=1),
        _bear_tools(tmp_path / "artifacts"),
        bus,
        tmp_path,
    )
    report = run.report

    shorts = [s for s in report.signals if s.direction == "SHORT"]
    longs = [s for s in report.signals if s.direction == "LONG"]
    assert shorts, "a hard downtrend must produce a SHORT signal"
    assert not longs, "a hard downtrend must not produce LONGs for the same symbol"
    sig = shorts[0]
    assert sig.symbol == "BEARUSD"
    assert sig.score >= 60
    assert sig.tp2 < sig.tp1 < sig.entry < sig.stop
    assert sig.citations and report.citation_coverage == 1.0
    # Phase 7.5 is long-only: the short keeps its daily plan, disclosed (R4)
    assert sig.entry_plan is None
    assert any("long-only" in d and "Phase 7.6" in d
                for d in report.disclosures), report.disclosures
    # ledger record: positive risk, SHORT direction
    rec = next(r for r in read_ledger(tmp_path / "signals.jsonl") if r.symbol == "BEARUSD")
    assert rec.direction == "SHORT"
    assert rec.risk > 0 and rec.stop > rec.entry


def read_ledger(path):
    from signaldesk.ledger import LedgerRecord

    return [LedgerRecord(**json.loads(line))
            for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]