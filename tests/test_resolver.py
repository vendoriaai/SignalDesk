"""Resolution pipeline (WF-5): ledger selection, bars provider, persistence."""
from types import SimpleNamespace

import pandas as pd

from signaldesk import ledger, resolver

DECISION_BAR = (100.0, 101.0, 80.0, 100.0)


def _bars(rows, start="2026-09-01"):
    dates = pd.date_range(start, periods=len(rows), freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
    })


def _record(**over) -> ledger.LedgerRecord:
    base = dict(signal_id="run1:BTCUSD", created_at="2026-09-02T00:00:00+00:00",
                run_id="run1", market="crypto", symbol="BTCUSD", score=80.0,
                entry=100.0, stop=95.0, tp1=110.0, tp2=115.0,
                risk=5.0, risk_pct=5.0, cost_pct=0.25, cost_in_r=0.05,
                bars_last_date="2026-09-01")
    base.update(over)
    return ledger.LedgerRecord(**base)


class _FakeOHLCVTool:
    """Offline stand-in for the yfinance/demo OHLCV tools (ToolResult shape)."""

    def __init__(self, artifacts_dir, market: str, bars: pd.DataFrame):
        self.artifacts_dir = artifacts_dir
        self.market = market
        self.bars = bars

    def run(self, symbol: str, period: str = "2y", interval: str = "1d"):
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"ohlcv_{symbol}_{interval}.csv"
        self.bars.to_csv(path, index=False)
        return SimpleNamespace(csv_files=[str(path)])


def test_run_resolution_persists_and_joins(tmp_path):
    records = [_record(), _record(signal_id="run1:ETHUSD", symbol="ETHUSD")]
    ledger.append_records(ledger.ledger_path(tmp_path), records)
    tool = _FakeOHLCVTool(tmp_path / "outcome_bars", "crypto",
                          _bars([DECISION_BAR, (100.0, 105.0, 99.0, 104.0),
                                 (104.0, 116.0, 103.0, 115.0)]))
    rows = resolver.run_resolution(tmp_path, tool_builder=lambda mkt: tool)

    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"tp2"}      # same bars as test_outcomes
    assert all(r["mode"] == "daily" and r["market"] == "crypto" for r in rows)
    assert (tmp_path / "outcomes.jsonl").is_file()


def test_run_resolution_empty_ledger_is_a_no_op(tmp_path):
    assert resolver.run_resolution(tmp_path) == []
    assert not (tmp_path / "outcomes.jsonl").exists()


def test_select_records_filters(tmp_path):
    records = [
        _record(),
        _record(signal_id="run1:ETHUSD", symbol="ETHUSD", demo=True),
        _record(signal_id="run1:EURUSD", symbol="EURUSD", market="forex"),
        _record(signal_id="run1:SOLUSD", symbol="SOLUSD", bars_last_date=""),
    ]
    ledger.append_records(ledger.ledger_path(tmp_path), records)
    keep = resolver.select_records(tmp_path, backfill=False)
    assert [r.signal_id for r in keep] == ["run1:BTCUSD", "run1:EURUSD"]
    assert [r.signal_id for r in resolver.select_records(tmp_path, backfill=False,
                                                         include_demo=True)] == \
        ["run1:BTCUSD", "run1:ETHUSD", "run1:EURUSD"]
    assert [r.signal_id for r in resolver.select_records(tmp_path, backfill=False,
                                                         market="forex")] == ["run1:EURUSD"]
    # demo-based runs keep demo records: the whole run is synthetic
    assert [r.signal_id for r in resolver.select_records(tmp_path, backfill=False,
                                                         demo=True)] == \
        ["run1:BTCUSD", "run1:ETHUSD", "run1:EURUSD"]


def test_signal_rows_newest_first_with_status(tmp_path):
    ledger.append_records(ledger.ledger_path(tmp_path), [
        _record(signal_id="run1:BTCUSD"),
        _record(signal_id="run0:ETHUSD", symbol="ETHUSD",
                created_at="2026-09-01T00:00:00+00:00"),
    ])
    rows = resolver.signal_rows(tmp_path)
    assert [r["signal_id"] for r in rows] == ["run1:BTCUSD", "run0:ETHUSD"]
    assert all(r["status"] == "open" and r["r_net"] is None for r in rows)

    import json

    (tmp_path / "outcomes.jsonl").write_text(
        json.dumps({"signal_id": "run0:ETHUSD", "status": "time", "r_net": 0.4,
                    "bars": 14, "resolved_at": "2026-09-10T00:00:00+00:00"}) + "\n",
        encoding="utf-8")
    rows = resolver.signal_rows(tmp_path)
    eth = next(r for r in rows if r["symbol"] == "ETHUSD")
    assert eth["status"] == "time" and eth["r_net"] == 0.4
