"""Outcome metrics: Wilson intervals, week-block bootstrap, R accounting."""
import pytest

from signaldesk import metrics


def test_wilson_interval_bounds_and_shape():
    assert metrics.wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = metrics.wilson_interval(0, 20)
    assert lo == 0.0 and 0 < hi < 0.2
    lo, hi = metrics.wilson_interval(20, 20)
    assert hi == 1.0 and 0.8 < lo < 1.0
    lo, hi = metrics.wilson_interval(55, 100)
    assert lo == pytest.approx(0.452, abs=0.01)
    assert hi == pytest.approx(0.644, abs=0.01)
    # more data narrows the interval
    wide = metrics.wilson_interval(55, 100)
    narrow = metrics.wilson_interval(550, 1000)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def _row(r_net, status="tp2", **over):
    base = dict(signal_id="20260901T000000Z:BTCUSD", symbol="BTCUSD", market="crypto",
                status=status, r_net=r_net, r_gross=r_net, r_all_in=r_net, r_runner=r_net,
                bars_to_tp1=3 if status in ("tp1", "tp2") else None,
                mfe_r=1.5, mae_r=-0.4, cost_in_r=0.05, mode="market")
    base.update(over)
    return base


def test_week_key_reads_the_run_stamp():
    assert metrics.week_key({"signal_id": "20260901T120000Z:BTCUSD"}) == "2026-W0901"
    assert metrics.week_key({"signal_id": "nonsense"}) == "unknown"


def test_block_bootstrap_is_deterministic_and_brackets_the_mean():
    rows = [_row(1.0), _row(2.0), _row(-1.0, status="sl"), _row(0.5, status="tp1")]
    ci = metrics.block_bootstrap_ci(rows)
    assert ci == metrics.block_bootstrap_ci(rows)          # seeded
    mean = sum(r["r_net"] for r in rows) / len(rows)
    assert ci[0] <= mean <= ci[1]
    assert metrics.block_bootstrap_ci([]) == (0.0, 0.0)


def test_summarize_core_statistics():
    rows = [
        _row(2.5, status="tp2", signal_id="20260901T000000Z:A"),
        _row(0.5, status="tp1", signal_id="20260901T000000Z:B"),
        _row(-1.05, status="sl", signal_id="20260908T000000Z:C"),
        _row(0.3, status="time", signal_id="20260908T000000Z:D", bars_to_tp1=None, mode="daily"),
        _row(1.0, status="open", signal_id="20260915T000000Z:E"),
    ]
    m = metrics.summarize(rows)
    assert m.n_rows == 5 and m.n_resolved == 4 and m.n_open == 1
    assert m.expectancy_r == pytest.approx(2.25 / 4, abs=1e-3)
    assert m.hit_rate == pytest.approx(0.5)                 # 2 of 4 reached TP1+
    assert m.censored_pct == pytest.approx(0.25)
    assert m.blocks == 2                                    # two distinct weeks
    assert m.profit_factor == pytest.approx(3.3 / 1.05, rel=1e-2)
    assert m.median_bars_to_tp1 == 3
    assert m.mean_cost_in_r == pytest.approx(0.05)
    assert m.by_mode["market"]["n"] == 3 and m.by_mode["daily"]["n"] == 1
    assert "confidence interval" in " ".join(m.notes)


def test_summarize_handles_empty_and_all_open():
    empty = metrics.summarize([])
    assert empty.n_resolved == 0 and "no resolved signals" in " ".join(empty.notes)
    assert metrics.render_text(empty).startswith("signals: 0 recorded")
    open_only = metrics.summarize([_row(0.0, status="open")])
    assert open_only.n_resolved == 0
    assert "no resolved signals" in " ".join(open_only.notes)


def test_render_text_reports_intervals_not_just_point_estimates():
    rows = [_row(2.5, status="tp2", signal_id="20260901T000000Z:A"),
            _row(-1.0, status="sl", signal_id="20260908T000000Z:B")]
    text = metrics.render_text(metrics.summarize(rows))
    assert "expectancy" in text and "95% CI" in text
    assert "hit rate" in text and "profit factor" in text
    assert "cost drag" in text
