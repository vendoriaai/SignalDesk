"""Signal ledger: records, dedupe, hashes, backfill, and workflow wiring."""
import json

from signaldesk import ledger
from signaldesk.report.schema import EntryPlan, ScanReport, Signal


def _signal(**over) -> Signal:
    base = dict(symbol="BTCUSD", score=80.0, entry=100.0, stop=95.0, tp1=110.0, tp2=115.0,
                cost_pct=0.25, cost_in_r=0.05)
    base.update(over)
    return Signal(**base)


def _report(signals=None) -> ScanReport:
    return ScanReport(
        market="crypto", as_of="2026-09-27T00:00:00+00:00",
        scoring_preset="trend-momentum-v1", universe=["BTCUSD"],
        context_summary="", signals=signals or [_signal()],
    )


def test_record_carries_levels_risk_and_provenance(tmp_path):
    recs = ledger.record_report(_report(), "20260927T000000Z", market="crypto",
                                data_hashes={"BTCUSD": "abc123"},
                                bars_last_dates={"BTCUSD": "2026-09-26"},
                                cost_pcts={"BTCUSD": 0.25})
    assert len(recs) == 1
    rec = recs[0]
    assert rec.signal_id == "20260927T000000Z:BTCUSD"
    assert rec.risk == 5.0
    assert rec.risk_pct == 5.0
    assert rec.cost_in_r == 0.05
    assert rec.data_hash == "abc123"
    assert rec.bars_last_date == "2026-09-26"
    assert rec.weights_hash and len(rec.weights_hash) == 16
    assert rec.source == "scan" and rec.demo is False


def test_record_uses_refined_plan_when_actionable():
    plan = EntryPlan(mode="market", entry=101.0, stop=97.25, tp1=108.5, tp2=112.25,
                     risk_daily=5.0, risk_refined=3.75)
    recs = ledger.record_report(_report([_signal(entry_plan=plan)]), "run1", market="crypto")
    assert recs[0].entry == 101.0 and recs[0].stop == 97.25
    assert recs[0].entry_mode == "market"
    assert recs[0].risk == 3.75


def test_wait_mode_keeps_the_daily_levels():
    plan = EntryPlan(mode="wait", entry=100.0, stop=95.0, tp1=110.0, tp2=115.0,
                     risk_daily=5.0, risk_refined=5.0)
    recs = ledger.record_report(_report([_signal(entry_plan=plan)]), "run2", market="crypto")
    assert recs[0].entry_mode == "daily" and recs[0].entry == 100.0


def test_append_is_idempotent_and_reads_back(tmp_path):
    path = tmp_path / "signals.jsonl"
    recs = ledger.record_report(_report(), "run1", market="crypto")
    assert ledger.append_records(path, recs) == 1
    assert ledger.append_records(path, recs) == 0          # same signal_id
    assert len(ledger.read_records(path)) == 1
    assert ledger.read_ids(path) == {"run1:BTCUSD"}


def test_read_skips_malformed_lines(tmp_path):
    path = tmp_path / "signals.jsonl"
    recs = ledger.record_report(_report(), "run1", market="crypto")
    path.write_text("{not json}\n" + recs[0].model_dump_json() + "\n\n", encoding="utf-8")
    assert len(ledger.read_records(path)) == 1


def test_default_ledger_path_follows_the_runs_layout(tmp_path):
    assert ledger.default_ledger_path(tmp_path / "runs" / "20260101T000000Z") == \
        tmp_path / "signals.jsonl"
    assert ledger.default_ledger_path(tmp_path / "adhoc") == tmp_path / "adhoc" / "signals.jsonl"
    assert ledger.ledger_path(tmp_path) == tmp_path / "signals.jsonl"


def test_sha256_file_is_stable_and_missing_safe(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("date,close\n2026-09-01,100\n", encoding="utf-8")
    once = ledger.sha256_file(f)
    assert once and once == ledger.sha256_file(f)
    f.write_text("date,close\n2026-09-01,101\n", encoding="utf-8")
    assert ledger.sha256_file(f) != once
    assert ledger.sha256_file(tmp_path / "missing.csv") == ""


def test_backfill_imports_past_reports(tmp_path):
    for run_id, marker in (("20260920T000000Z", 0), ("20260921T000000Z-demo", 1)):
        run_dir = tmp_path / "runs" / run_id
        run_dir.mkdir(parents=True)
        report = _report([_signal(score=70.0 + marker)])
        (run_dir / "report.json").write_text(
            json.dumps(report.model_dump(mode="json")), encoding="utf-8")
    assert ledger.backfill(tmp_path) == 2
    recs = {r.run_id: r for r in ledger.read_records(ledger.ledger_path(tmp_path))}
    assert set(recs) == {"20260920T000000Z", "20260921T000000Z-demo"}
    assert recs["20260920T000000Z"].source == "backfill"
    assert recs["20260920T000000Z"].bars_last_date == "2026-09-27"
    assert recs["20260921T000000Z-demo"].demo is True
    assert recs["20260920T000000Z"].data_hash == ""      # no snapshot available


def test_backfill_filters_by_market(tmp_path):
    run_dir = tmp_path / "runs" / "20260920T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps(_report().model_dump(mode="json")), encoding="utf-8")
    assert ledger.backfill(tmp_path, market="forex") == 0
    assert ledger.backfill(tmp_path, market="crypto") == 1


def test_scan_writes_ledger_records(tmp_path):
    """End-to-end: a (demo) scan leaves a ledger beside the run."""
    from signaldesk.agent.events import EventBus
    from signaldesk.tools import demo
    from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

    d = tmp_path / "artifacts"
    tools = ToolSet(
        movers=demo.DemoMoversTool(d, "crypto"), quotes=demo.DemoQuotesTool(d, "crypto"),
        ohlcv=demo.DemoOHLCVTool(d, "crypto"), search=demo.DemoSearchTool(),
        fear_greed=demo.DemoFearGreedTool(d), altseason=demo.DemoAltSeasonTool(d),
    )
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=4),
                          tools, EventBus(), tmp_path)
    path = ledger.default_ledger_path(tmp_path)
    records = ledger.read_records(path)
    assert len(records) == len(run.report.signals) > 0
    for rec in records:
        assert rec.demo is True                     # demo data is flagged, not hidden
        assert rec.data_hash and rec.bars_last_date
        assert rec.cost_pct > 0 and rec.risk > 0
        assert rec.weights_hash
    assert {r.symbol for r in records} == {s.symbol for s in run.report.signals}
