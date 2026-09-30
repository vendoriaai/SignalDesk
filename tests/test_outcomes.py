"""Triple-barrier resolver: barrier order, policy R variants, MFE/MAE, storage."""
import pandas as pd
import pytest

from signaldesk import ledger, outcomes


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


DECISION_BAR = (100.0, 101.0, 80.0, 100.0)  # low=80: would be a stop-out if not ignored


def test_tp2_hit_and_declared_policy_r():
    bars = _bars([DECISION_BAR, (100.0, 105.0, 99.0, 104.0), (104.0, 116.0, 103.0, 115.0)])
    res = outcomes.resolve(_record(), bars)
    assert res.status == "tp2"
    assert res.bars == 2 and res.bars_to_tp1 == 2 and res.bars_to_tp2 == 2
    assert res.exit_price == 115.0
    assert res.r_gross == pytest.approx(2.5)      # 50% at 2R + 50% at 3R
    assert res.r_all_in == pytest.approx(2.0)
    assert res.r_runner == pytest.approx(3.0)
    assert res.r_net == pytest.approx(2.45)       # minus 0.05R cost
    assert res.mfe_r == pytest.approx(3.2)        # 116 -> (116-100)/5
    assert res.mae_r == pytest.approx(-0.2)


def test_decision_bar_is_ignored():
    """The 80 low on the decision bar must not count as a stop-out."""
    bars = _bars([DECISION_BAR, (100.0, 101.0, 99.5, 100.5)])
    res = outcomes.resolve(_record(), bars)
    assert res.status == "time"
    assert res.bars == 1


def test_stop_first_is_pessimistic_same_bar():
    bars = _bars([DECISION_BAR, (100.0, 111.0, 94.0, 96.0)])  # tp1 and stop in one bar
    res = outcomes.resolve(_record(), bars)
    assert res.status == "sl"
    assert res.bars_to_tp1 is None
    assert res.r_gross == pytest.approx(-1.0)
    assert res.r_all_in == pytest.approx(-1.0)
    assert res.r_net == pytest.approx(-1.05)


def test_tp1_then_stop_pays_half_position():
    bars = _bars([DECISION_BAR, (100.0, 111.0, 99.0, 104.0), (104.0, 105.0, 94.0, 95.0)])
    res = outcomes.resolve(_record(), bars)
    assert res.status == "tp1"
    assert res.bars == 2 and res.bars_to_tp1 == 1 and res.bars_to_tp2 is None
    assert res.r_gross == pytest.approx(0.5)      # 1.0R banked + half of -1R
    assert res.r_all_in == pytest.approx(2.0)
    assert res.r_runner == pytest.approx(-1.0)


def test_time_barrier_marks_to_market_and_respects_horizon():
    bars = _bars([DECISION_BAR, (100.0, 101.0, 99.0, 101.0), (101.0, 103.0, 100.0, 102.0),
                  (102.0, 120.0, 101.0, 119.0)])
    res = outcomes.resolve(_record(), bars, horizon_bars=2)
    assert res.status == "time"                   # the 120 high is outside the horizon
    assert res.exit_price == 102.0
    assert res.r_gross == pytest.approx(0.4)
    assert res.r_net == pytest.approx(0.35)
    assert res.bars_last == "2026-09-03"


def test_open_when_no_bars_after_the_decision_bar():
    bars = _bars([DECISION_BAR])                  # the decision bar is the latest bar
    assert outcomes.resolve(_record(), bars).status == "open"
    two = _bars([DECISION_BAR, (100.0, 101.0, 99.5, 100.5)])
    assert outcomes.resolve(_record(), two).status == "time"


def test_no_data_and_degenerate_risk():
    assert outcomes.resolve(_record(), pd.DataFrame()).status == "no_data"
    assert outcomes.resolve(_record(risk=0.0, stop=100.0), _bars([DECISION_BAR])).status == "no_data"
    # with no anchor the decision bar itself becomes the first tradable bar
    # (its low of 80 stops the trade out) — the reason records carry a bar date
    assert outcomes.resolve(_record(bars_last_date=""), _bars([DECISION_BAR])).status == "sl"


def test_cost_in_r_flows_into_net_r():
    bars = _bars([DECISION_BAR, (100.0, 105.0, 99.0, 104.0), (104.0, 116.0, 103.0, 115.0)])
    free = outcomes.resolve(_record(cost_in_r=0.0), bars)
    costly = outcomes.resolve(_record(cost_in_r=0.5), bars)
    assert free.r_net == pytest.approx(free.r_gross)
    assert costly.r_net == pytest.approx(costly.r_gross - 0.5)
    assert costly.cost_in_r == 0.5


def test_bars_provider_from_tool_reads_the_artifact(tmp_path):
    from signaldesk.tools import demo

    provider = outcomes.bars_provider_from_tool(demo.DemoOHLCVTool(tmp_path, "crypto"))
    frame = provider("BTCUSD")
    assert not frame.empty and {"date", "high", "low", "close"} <= set(frame.columns)


def test_write_and_load_latest_keeps_newest(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    first = outcomes.resolve(_record(), _bars([DECISION_BAR, (100.0, 101.0, 99.0, 100.5)]))
    first.resolved_at = "2026-09-10T00:00:00+00:00"
    newer = outcomes.resolve(_record(), _bars([DECISION_BAR, (100.0, 116.0, 99.0, 115.0)]))
    newer.resolved_at = "2026-09-11T00:00:00+00:00"
    assert outcomes.write_resolutions(path, [first, newer]) == 2
    latest = outcomes.load_latest(path)
    assert latest["run1:BTCUSD"]["status"] == "tp2"
    assert outcomes.load_latest(tmp_path / "missing.jsonl") == {}


def test_resolve_records_fetches_bars_once_per_symbol():
    calls: list[str] = []

    def provider(symbol: str) -> pd.DataFrame:
        calls.append(symbol)
        return _bars([DECISION_BAR, (100.0, 105.0, 99.0, 104.0), (104.0, 116.0, 103.0, 115.0)])

    recs = [_record(signal_id="r1:BTCUSD"), _record(signal_id="r2:BTCUSD"),
            _record(signal_id="r3:ETHUSD", symbol="ETHUSD")]
    out = outcomes.resolve_records(recs, provider)
    assert [r.status for r in out] == ["tp2", "tp2", "tp2"]
    assert calls == ["BTCUSD", "ETHUSD"]


# --- SHORT side: mirrored barriers (same conventions, opposite direction) --------

def _short_record(**over) -> ledger.LedgerRecord:
    base = dict(direction="SHORT", entry=100.0, stop=105.0, tp1=90.0, tp2=85.0)
    base.update(over)
    return _record(**base)


def test_short_tp2_hit_and_declared_policy_r():
    bars = _bars([DECISION_BAR, (100.0, 101.0, 84.0, 86.0)])   # low 84 <= tp2 85
    res = outcomes.resolve(_short_record(), bars)
    assert res.direction == "SHORT"
    assert res.status == "tp2"
    assert res.bars == 1 and res.bars_to_tp1 == 1 and res.bars_to_tp2 == 1
    assert res.exit_price == 85.0
    assert res.r_gross == pytest.approx(2.5)
    assert res.r_all_in == pytest.approx(2.0)
    assert res.r_runner == pytest.approx(3.0)
    assert res.r_net == pytest.approx(2.45)
    assert res.mfe_r == pytest.approx(3.2)        # low 84: (100-84)/5
    assert res.mae_r == pytest.approx(-0.2)       # high 101: (100-101)/5


def test_short_stop_loss_on_high():
    bars = _bars([DECISION_BAR, (100.0, 106.0, 99.0, 101.0)])  # high 106 >= stop 105
    res = outcomes.resolve(_short_record(), bars)
    assert res.status == "sl"
    assert res.exit_price == 105.0
    assert res.r_gross == pytest.approx(-1.0)
    assert res.r_net == pytest.approx(-1.05)


def test_short_same_bar_tie_stop_wins():
    bars = _bars([DECISION_BAR, (100.0, 105.5, 84.0, 100.0)])  # stop and tp1 in one bar
    res = outcomes.resolve(_short_record(), bars)
    assert res.status == "sl"
    assert res.bars_to_tp1 is None


def test_short_tp1_then_stop_pays_half_position():
    bars = _bars([DECISION_BAR, (100.0, 101.0, 89.0, 95.0),    # low 89 <= tp1 90
                  (95.0, 106.0, 94.0, 95.0)])                  # high 106 >= stop
    res = outcomes.resolve(_short_record(), bars)
    assert res.status == "tp1"
    assert res.bars_to_tp1 == 1 and res.bars_to_tp2 is None
    assert res.r_gross == pytest.approx(0.5)
    assert res.r_runner == pytest.approx(-1.0)


def test_short_time_barrier_marks_to_market():
    bars = _bars([DECISION_BAR, (100.0, 101.0, 99.0, 101.0), (101.0, 103.0, 100.0, 102.0)])
    res = outcomes.resolve(_short_record(), bars, horizon_bars=2)
    assert res.status == "time"
    assert res.exit_price == 102.0
    assert res.r_gross == pytest.approx(-0.4)     # (100 - 102)/5
    assert res.r_net == pytest.approx(-0.45)


def test_short_risk_falls_back_to_abs_entry_stop():
    rec = _short_record(risk=0.0)
    bars = _bars([DECISION_BAR, (100.0, 106.0, 99.0, 101.0)])
    res = outcomes.resolve(rec, bars)
    assert res.status == "sl"                     # risk = |100 - 105| = 5 resolves fine
