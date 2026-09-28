"""Paper execution log: fill/miss decisions, entry gap, funding drag."""
import pytest

from signaldesk import ledger, paper


def _record(**over) -> ledger.LedgerRecord:
    base = dict(signal_id="run1:BTCUSD", created_at="2026-09-02T00:00:00+00:00",
                run_id="run1", market="crypto", symbol="BTCUSD", score=80.0,
                entry=100.0, stop=95.0, tp1=110.0, tp2=115.0,
                risk=5.0, risk_pct=5.0, cost_pct=0.25, cost_in_r=0.05,
                bars_last_date="2026-09-01")
    base.update(over)
    return ledger.LedgerRecord(**base)


def test_log_event_requires_a_known_signal(tmp_path):
    with pytest.raises(KeyError):
        paper.log_event(tmp_path, "nope:BTCUSD", "fill", price=101.0)


def test_only_fills_carry_a_price(tmp_path):
    ledger.append_records(ledger.ledger_path(tmp_path), [_record()])
    with pytest.raises(ValueError):
        paper.log_event(tmp_path, "run1:BTCUSD", "miss", price=101.0)
    with pytest.raises(ValueError):
        paper.log_event(tmp_path, "run1:BTCUSD", "skip", price=101.0)


def test_first_decision_wins_and_later_events_never_relabel(tmp_path):
    rec = _record()
    ledger.append_records(ledger.ledger_path(tmp_path), [rec])
    paper.log_event(tmp_path, rec.signal_id, "fill", price=101.0)
    paper.log_event(tmp_path, rec.signal_id, "miss")       # misclick / change of mind
    first = paper.first_events(tmp_path)
    assert first[rec.signal_id].event == "fill"
    assert paper.read_events(paper.paper_path(tmp_path))[1].event == "miss"  # history kept
    stats = paper.paper_stats(tmp_path, {rec.signal_id: rec}, {})
    assert stats["fills"] == 1 and stats["misses"] == 0 and stats["fill_rate"] == 1.0


def test_fill_rate_counts_misses(tmp_path):
    records = [_record(), _record(signal_id="run1:ETHUSD", symbol="ETHUSD")]
    ledger.append_records(ledger.ledger_path(tmp_path), records)
    paper.log_event(tmp_path, records[0].signal_id, "fill", price=101.0)
    paper.log_event(tmp_path, records[1].signal_id, "miss")
    stats = paper.paper_stats(tmp_path, {r.signal_id: r for r in records}, {})
    assert stats["fills"] == 1 and stats["misses"] == 1 and stats["fill_rate"] == 0.5
    assert stats["mean_entry_gap_r"] == pytest.approx(-0.2)   # filled 1R/5 above entry


def test_entry_gap_r_is_direction_aware():
    rec = _record()                                            # LONG: entry 100, risk 5
    assert paper.entry_gap_r(rec, 101.0) == pytest.approx(-0.2)  # paid more: worse
    assert paper.entry_gap_r(rec, 99.0) == pytest.approx(0.2)    # filled lower: better
    short = _record(direction="SHORT", stop=105.0)
    assert paper.entry_gap_r(short, 101.0) == pytest.approx(0.2)
    assert paper.entry_gap_r(short, 99.0) == pytest.approx(-0.2)


def test_funding_drag_applies_to_crypto_only(tmp_path):
    records = [_record(), _record(signal_id="run1:EURUSD", symbol="EURUSD", market="forex")]
    ledger.append_records(ledger.ledger_path(tmp_path), records)
    paper.log_event(tmp_path, records[0].signal_id, "fill", price=100.0)
    paper.log_event(tmp_path, records[1].signal_id, "fill", price=1.08)
    latest = {r.signal_id: {"status": "time", "bars": 10} for r in records}
    stats = paper.paper_stats(tmp_path, {r.signal_id: r for r in records}, latest)
    # 4%/yr funding over 10 days on a 5%-risk unit: 4.0/365*10 / 5 = ~0.022R
    assert stats["mean_funding_r"] == pytest.approx(4.0 / 365 * 10 / 5.0, abs=5e-4)
    assert stats["n_funding"] == 1                             # forex has no funding


def test_unresolved_signals_contribute_no_funding(tmp_path):
    rec = _record()
    ledger.append_records(ledger.ledger_path(tmp_path), [rec])
    paper.log_event(tmp_path, rec.signal_id, "fill", price=100.0)
    stats = paper.paper_stats(tmp_path, {rec.signal_id: rec},
                              {rec.signal_id: {"status": "open", "bars": 0}})
    assert stats["n_funding"] == 0


def test_read_events_skips_malformed_lines(tmp_path):
    ledger.append_records(ledger.ledger_path(tmp_path), [_record()])
    paper.log_event(tmp_path, "run1:BTCUSD", "miss")
    path = paper.paper_path(tmp_path)
    path.write_text("{broken json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    events = paper.read_events(path)
    assert len(events) == 1 and events[0].event == "miss"
