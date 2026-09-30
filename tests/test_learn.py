"""Learning layer: dataset builder, logistic fit, walk-forward, shadow scoring."""
import json
import math

import numpy as np
import pytest

from signaldesk import learn
from signaldesk.ledger import LedgerRecord


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

def _record(signal_id="run1:BTCUSD", *, market="crypto", symbol="BTCUSD",
            created="2026-01-01T00:00:00+00:00", bars_last="2026-01-01",
            context=None, demo=False, entry_mode="daily", score=75.0,
            ml_score=None, ml_fingerprint=""):
    return LedgerRecord(
        signal_id=signal_id, created_at=created, as_of=created,
        run_id=signal_id.split(":")[0],
        market=market, symbol=symbol, score=score, preset="",
        entry=100.0, stop=95.0, tp1=110.0, tp2=115.0,
        entry_mode=entry_mode, risk=5.0, risk_pct=5.0,
        cost_pct=0.25, cost_in_r=0.1, bars_last_date=bars_last,
        demo=demo, context=context or {},
        ml_score=ml_score, ml_fingerprint=ml_fingerprint)


def _res(signal_id, r_net, status="tp2"):
    return {"signal_id": signal_id, "status": status, "r_net": r_net}


def _row(i, *, win, rsi=None, source="scan"):
    """A learning row whose label is driven by rsi (deterministic pattern)."""
    date = f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d}"
    feats = {
        "score": 70.0 + (5.0 if win else 0.0),
        "risk_pct": 5.0, "cost_in_r": 0.1,
        "entry_mode_market": 1.0 if i % 4 == 0 else 0.0,
        "entry_mode_pullback": 0.0,
        "change_24h_pct": 2.0, "volume_ratio": 1.5,
        "rsi14": rsi if rsi is not None else 55.0,
        "sma20_dist_pct": 2.0, "sma50_dist_pct": 6.0,
        "btc_mom_20d_pct": 4.0, "btc_mom_missing": 0.0,
    }
    return learn.LearningRow(
        signal_id=f"run{i}:S", decision_date=date, market="crypto",
        symbol=f"S{i}", source=source,
        r_net=(1.6 if win else -1.0), label=1 if win else 0, features=feats)


def _pattern_rows(n=40):
    # rsi 40-ish wins, rsi 60-ish loses: clean, learnable, deterministic
    return [_row(i, win=(i % 3 != 0), rsi=40 + (i % 5) if (i % 3 != 0) else 62 - (i % 5))
            for i in range(n)]


# --------------------------------------------------------------------------
# feature vector
# --------------------------------------------------------------------------

def test_feature_vector_maps_context_and_missing_flags():
    rec = _record(context={"rsi14": 55.0, "sma20_dist_pct": 3.0, "btc_mom_20d_pct": 4.0})
    vec = learn.feature_vector(rec)
    assert vec["rsi14"] == 55.0
    assert vec["sma20_dist_pct"] == 3.0
    assert vec["btc_mom_20d_pct"] == 4.0
    assert vec["btc_mom_missing"] == 0.0
    assert vec["entry_mode_market"] == 0.0 and vec["entry_mode_pullback"] == 0.0
    assert math.isnan(vec["change_24h_pct"])          # missing -> NaN, never guessed


def test_feature_vector_flags_missing_btc_momentum_and_entry_mode():
    rec = _record(entry_mode="market", context={})
    vec = learn.feature_vector(rec)
    assert vec["btc_mom_missing"] == 1.0
    assert vec["entry_mode_market"] == 1.0
    assert all(math.isnan(vec[k]) for k in learn.CONTEXT_FEATURES)


def test_feature_vector_ignores_nonfinite_context_values():
    rec = _record(context={"rsi14": float("inf")})
    vec = learn.feature_vector(rec)
    assert math.isnan(vec["rsi14"])


# --------------------------------------------------------------------------
# dataset builder
# --------------------------------------------------------------------------

def test_build_dataset_joins_labels_and_filters():
    records = [
        _record("r1:A", bars_last="2026-01-02"),
        _record("r2:B", demo=True),                       # excluded: demo
        _record("r3:C"),                                  # excluded: no resolution
    ]
    resolutions = {
        "r1:A": _res("r1:A", 1.8),
        "r2:B": _res("r2:B", -1.0),                       # resolved but demo
        "r3:C": _res("r3:C", 0.0, status="open"),         # excluded: open
    }
    rows = learn.build_dataset(records, resolutions)
    assert [r.signal_id for r in rows] == ["r1:A"]
    assert rows[0].label == 1 and rows[0].r_net == 1.8


def test_build_dataset_drops_open_and_no_data_resolutions():
    records = [_record(f"r{i}:S{i}", bars_last=f"2026-01-0{i}") for i in range(1, 4)]
    resolutions = {
        "r1:S1": _res("r1:S1", -1.0, status="sl"),
        "r2:S2": _res("r2:S2", 0.5, status="open"),
        "r3:S3": _res("r3:S3", 0.5, status="no_data"),
    }
    rows = learn.build_dataset(records, resolutions)
    assert [r.signal_id for r in rows] == ["r1:S1"]


def test_build_dataset_sorts_chronologically():
    records = [
        _record("b:BTC", bars_last="2026-02-01"),
        _record("a:BTC", bars_last="2026-01-01"),
    ]
    resolutions = {"b:BTC": _res("b:BTC", 1.0), "a:BTC": _res("a:BTC", -1.0)}
    rows = learn.build_dataset(records, resolutions)
    assert [r.decision_date for r in rows] == ["2026-01-01", "2026-02-01"]


def test_load_dataset_reports_counts(tmp_path):
    recs = [_record("r1:A", demo=False), _record("r2:B", demo=True)]
    (tmp_path / "signals.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in recs) + "\n", encoding="utf-8")
    (tmp_path / "outcomes.jsonl").write_text(
        json.dumps(_res("r1:A", 1.5)) + "\n"
        + json.dumps(_res("r1:A", 2.0, status="tp1")) + "\n",   # latest wins
        encoding="utf-8")
    rows, counts = learn.load_dataset(tmp_path)
    assert counts == {"ledger": 2, "demo": 1, "labeled": 1, "excluded": 1}
    assert rows[0].r_net == 2.0                          # load_latest, not first


# --------------------------------------------------------------------------
# logistic fit / train
# --------------------------------------------------------------------------

def test_fit_logistic_separates_linearly():
    X = np.array([[0.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 1.0],
                  [0.0, 0.0], [1.0, 1.0]])
    y = np.array([0.0, 0.0, 1.0, 1.0, 0.0, 1.0])
    w, b = learn._fit_logistic(X, y, l2=0.1)
    p = learn._sigmoid(X @ w + b)
    assert all(0.0 < v < 1.0 for v in p)
    assert p.mean(where=y == 1) > p.mean(where=y == 0)   # ranks the positives higher
    assert p[2] > p[0] and p[3] > p[1]                   # x1 drives the label


def test_train_refuses_empty_and_single_class():
    with pytest.raises(learn.LearningError):
        learn.train([])
    with pytest.raises(learn.LearningError, match="only losses"):
        learn.train([_row(i, win=False) for i in range(25)])


def test_train_walk_forward_is_out_of_sample():
    rows = _pattern_rows(40)
    model = learn.train(rows, min_train=20)
    wf = model["walk_forward"]
    assert wf["n_scored"] == 20                          # rows 20..39, each scored blind
    assert wf["auc"] >= 0.8                              # the pattern is learnable
    assert wf["log_loss"] < wf["base_log_loss"]          # beats the base-rate guess
    terciles = {t["band"]: t for t in wf["terciles"]}
    assert terciles["high"]["expectancy_r"] > terciles["low"]["expectancy_r"]
    assert terciles["high"]["p_range"][0] > terciles["low"]["p_range"][1]


def test_train_final_model_scores_and_fingerprint_is_stable():
    rows = _pattern_rows(40)
    model = learn.train(rows, min_train=20)
    assert 0.0 <= learn._predict(model, rows[0].features) <= 1.0
    again = learn.train(rows, min_train=20)
    assert again["fingerprint"] == model["fingerprint"]  # same data -> same model id
    assert again["fingerprint"] != "" and len(again["fingerprint"]) == 16


def test_score_uses_stored_imputation_for_missing_context():
    rows = _pattern_rows(40)
    model = learn.train(rows, min_train=20)
    bare = learn.score_record(model, _record(context={}))
    with_ctx = learn.score_record(model, _record(context={"rsi14": 55.0}))
    assert 0.0 < bare < 1.0                              # imputed, still a probability
    assert bare != with_ctx                              # context actually moves the score


def test_scoring_survives_nan_context_values():
    rows = _pattern_rows(40)
    model = learn.train(rows, min_train=20)
    rec = _record(context={"rsi14": float("nan"), "sma20_dist_pct": 2.0})
    assert 0.0 <= learn.score_record(model, rec) <= 1.0


# --------------------------------------------------------------------------
# persistence / shadow scoring
# --------------------------------------------------------------------------

def test_save_load_roundtrip_and_corrupt_file(tmp_path):
    model = learn.train(_pattern_rows(40), min_train=20)
    path = learn.save_model(tmp_path / "learn_model.json", model)
    loaded = learn.load_model(path)
    assert loaded["fingerprint"] == model["fingerprint"]
    rec = _record(context={"rsi14": 50.0})
    assert learn.score_record(loaded, rec) == learn.score_record(model, rec)

    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert learn.load_model(tmp_path / "broken.json") is None
    assert learn.load_model(tmp_path / "missing.json") is None


def test_default_model_path_mirrors_ledger_layout(tmp_path):
    assert learn.default_model_path(tmp_path / "runs" / "20260101T000000Z") == \
        tmp_path / "learn_model.json"
    assert learn.default_model_path(tmp_path / "adhoc") == tmp_path / "adhoc" / "learn_model.json"


def test_apply_shadow_scores_stamps_records():
    model = learn.train(_pattern_rows(40), min_train=20)
    recs = [_record("r1:A", context={"rsi14": 40.0}), _record("r2:B", context={"rsi14": 62.0})]
    n, fp = learn.apply_shadow_scores(recs, model)
    assert (n, fp) == (2, model["fingerprint"])
    assert all(0.0 <= r.ml_score <= 1.0 for r in recs)
    assert all(r.ml_fingerprint == model["fingerprint"] for r in recs)
    assert recs[0].ml_score > recs[1].ml_score           # low rsi wins more often


def test_apply_shadow_scores_with_no_model_is_a_noop():
    recs = [_record("r1:A")]
    n, fp = learn.apply_shadow_scores(recs, None)
    assert (n, fp) == (0, "")
    assert recs[0].ml_score is None and recs[0].ml_fingerprint == ""


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def test_shadow_report_without_model_degrades(tmp_path):
    rep = learn.shadow_report(tmp_path)
    assert rep["model"] is None
    assert rep["ledger"]["labeled"] == 0
    assert learn.render_report_text(rep).startswith("no trained model")


def test_shadow_report_with_model_flags_in_sample(tmp_path):
    rows = _pattern_rows(40)
    recs, resolutions = [], {}
    for i, row in enumerate(rows):
        rec = _record(row.signal_id, bars_last=row.decision_date,
                      context={"rsi14": row.features["rsi14"]})
        recs.append(rec)
        resolutions[row.signal_id] = _res(row.signal_id, row.r_net)
    (tmp_path / "signals.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in recs) + "\n", encoding="utf-8")
    (tmp_path / "outcomes.jsonl").write_text(
        "\n".join(json.dumps(v) for v in resolutions.values()) + "\n", encoding="utf-8")
    learn.save_model(learn.model_path(tmp_path), learn.train(rows, min_train=20))

    rep = learn.shadow_report(tmp_path)
    assert rep["model"]["n_train"] == 40
    assert rep["n_ml_scored"] == 0                       # no scan has stamped scores yet
    assert rep["history_terciles_in_sample"]             # in-sample view exists...
    assert "walk_forward" in rep                          # ...but walk-forward stays the judge
    text = learn.render_report_text(rep)
    assert "walk-forward" in text and "IN-SAMPLE" in text
