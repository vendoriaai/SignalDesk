"""Live mark-to-market for open signals (WF-5): unrealized R snapshots."""
import json
import pathlib
import tempfile

import pandas as pd

from signaldesk import ledger, mtm, outcomes


def _rec(signal_id: str, symbol: str, *, direction: str = "LONG",
         entry: float = 100.0, stop: float = 95.0) -> ledger.LedgerRecord:
    risk = entry - stop
    return ledger.LedgerRecord(
        signal_id=signal_id, created_at="2026-09-20T00:00:00+00:00",
        run_id=signal_id.split(":")[0], market="crypto", symbol=symbol,
        direction=direction, score=80.0, entry=entry, stop=stop,
        tp1=entry + 2 * risk, tp2=entry + 3 * risk,
        risk=risk, risk_pct=risk / entry * 100.0, cost_pct=0.25, cost_in_r=0.05,
        bars_last_date="2026-09-19",
    )


def _resolve(data_dir, signal_id: str, status: str) -> None:
    res = outcomes.Resolution(signal_id=signal_id, symbol=signal_id.split(":")[1],
                              status=status)
    outcomes.write_resolutions(outcomes.outcomes_path(data_dir), [res])


def _quotes_builder(prices: dict, fail: bool = False):
    class _Stub:
        def run(self, symbols):
            if fail:
                raise RuntimeError("offline")
            frame = pd.DataFrame([{"symbol": s, "price": prices[s]}
                                  for s in symbols if s in prices])
            path = pathlib.Path(tempfile.mkdtemp()) / "quotes.csv"
            frame.to_csv(path, index=False)
            from signaldesk.tools.base import Source, ToolResult
            return ToolResult(csv_files=[path], summary="stub",
                              sources=[Source(name="stub")])

    return lambda market: _Stub()


# unrealized R ----------------------------------------------------------------

def test_unrealized_r_is_direction_aware():
    rec = {"entry": 100.0, "stop": 95.0, "risk": 5.0, "direction": "LONG"}
    assert mtm.unrealized_r(rec, 102.0) == 0.4      # +2 points = +0.4R
    assert mtm.unrealized_r(rec, 90.0) == -2.0
    short = {"entry": 100.0, "stop": 105.0, "risk": 5.0, "direction": "SHORT"}
    assert mtm.unrealized_r(short, 98.0) == 0.4     # below entry is good for a short
    assert mtm.unrealized_r(short, 103.0) == -0.6


def test_unrealized_r_needs_a_risk_unit():
    assert mtm.unrealized_r({"entry": 100.0, "stop": 100.0, "risk": 0.0,
                             "direction": "LONG"}, 102.0) is None


# refresh ---------------------------------------------------------------------

def test_refresh_quotes_open_signals_and_persists(tmp_path):
    records = [_rec("r1:BTCUSD", "BTCUSD"), _rec("r2:ETHUSD", "ETHUSD"),
               _rec("r3:SOLUSD", "SOLUSD")]
    ledger.append_records(ledger.ledger_path(tmp_path), records)
    _resolve(tmp_path, "r3:SOLUSD", "sl")          # resolved rows are not quoted

    payload = mtm.refresh(tmp_path, tool_builder=_quotes_builder({
        "BTCUSD": 102.0, "ETHUSD": 2600.0, "SOLUSD": 999.0}))

    assert payload is not None and set(payload["rows"]) == {"r1:BTCUSD", "r2:ETHUSD"}
    assert payload["rows"]["r1:BTCUSD"]["r_unrealized"] == 0.4
    assert payload["rows"]["r1:BTCUSD"]["price"] == 102.0
    assert payload["fetched_at"]
    saved = mtm.load_mtm(tmp_path)
    assert saved["rows"]["r2:ETHUSD"]["symbol"] == "ETHUSD"


def test_short_signal_marks_against_entry(tmp_path):
    records = [_rec("r1:XRPUSD", "XRPUSD", direction="SHORT", entry=2.0, stop=2.1)]
    ledger.append_records(ledger.ledger_path(tmp_path), records)
    payload = mtm.refresh(tmp_path, tool_builder=_quotes_builder({"XRPUSD": 1.9}))
    assert payload["rows"]["r1:XRPUSD"]["r_unrealized"] == 1.0   # 0.1 below entry = +1R


def test_empty_quotes_keep_previous_snapshot(tmp_path):
    ledger.append_records(ledger.ledger_path(tmp_path), [_rec("r1:BTCUSD", "BTCUSD")])
    mtm.refresh(tmp_path, tool_builder=_quotes_builder({"BTCUSD": 102.0}))
    before = mtm.load_mtm(tmp_path)

    # tool succeeds but returns no usable prices (throttled batch): keep old
    result = mtm.refresh(tmp_path, tool_builder=_quotes_builder({}))
    assert result is None
    assert mtm.load_mtm(tmp_path) == before


def test_partial_quotes_update_only_quoted_signals(tmp_path):
    ledger.append_records(ledger.ledger_path(tmp_path),
                          [_rec("r1:BTCUSD", "BTCUSD"), _rec("r2:ETHUSD", "ETHUSD")])
    payload = mtm.refresh(tmp_path, tool_builder=_quotes_builder({"BTCUSD": 101.0}))
    assert set(payload["rows"]) == {"r1:BTCUSD"}


def test_degraded_refresh_keeps_previous_snapshot(tmp_path):
    ledger.append_records(ledger.ledger_path(tmp_path), [_rec("r1:BTCUSD", "BTCUSD")])
    mtm.refresh(tmp_path, tool_builder=_quotes_builder({"BTCUSD": 102.0}))
    before = mtm.load_mtm(tmp_path)

    result = mtm.refresh(tmp_path, tool_builder=_quotes_builder({}, fail=True))
    assert result is None
    assert mtm.load_mtm(tmp_path) == before        # old snapshot intact (R4)


def test_refresh_with_no_open_signals_clears_rows(tmp_path):
    payload = mtm.refresh(tmp_path, tool_builder=_quotes_builder({}))
    assert payload["rows"] == {} and payload["fetched_at"]
    assert mtm.load_mtm(tmp_path)["rows"] == {}


def test_load_mtm_missing_or_malformed(tmp_path):
    assert mtm.load_mtm(tmp_path) == {}
    (tmp_path / "mtm.json").write_text("{not json", encoding="utf-8")
    assert mtm.load_mtm(tmp_path) == {}


# open-signal selection --------------------------------------------------------

def test_open_signals_only_unresolved_or_open(tmp_path):
    records = [_rec("r1:BTCUSD", "BTCUSD"), _rec("r2:ETHUSD", "ETHUSD"),
               _rec("r3:SOLUSD", "SOLUSD")]
    ledger.append_records(ledger.ledger_path(tmp_path), records)
    _resolve(tmp_path, "r3:SOLUSD", "tp2")
    _resolve(tmp_path, "r1:BTCUSD", "open")        # explicitly open in outcomes.jsonl
    rows = mtm.open_signals(tmp_path)
    assert {r["signal_id"] for r in rows} == {"r1:BTCUSD", "r2:ETHUSD"}


def test_mtm_file_is_separate_from_resolution_state(tmp_path):
    (tmp_path / "outcomes_state.json").write_text(json.dumps({"last_status": "ok"}),
                                                  encoding="utf-8")
    mtm.refresh(tmp_path, tool_builder=_quotes_builder({}))
    state = json.loads((tmp_path / "outcomes_state.json").read_text(encoding="utf-8"))
    assert state == {"last_status": "ok"}
