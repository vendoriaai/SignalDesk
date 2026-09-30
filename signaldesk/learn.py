"""Meta-label learning layer (shadow mode — Phase 0/1 of the learning loop).

The rule-based planner stays the signal source. This module learns exactly one
thing from the measurement layer: given a signal the planner already emitted,
what is the probability that its r_net (the frozen triple-barrier conventions
in `outcomes.py`) is positive? That probability is recorded on the ledger
record (`ml_score`) and acts on nothing — a learned score may gate or size
signals only after a trial (`trials.py`) closes positive on the shadow
evidence (root rules R7 and R6).

Design rules:
- Labels are read from `outcomes.jsonl` exactly as resolved; nothing here
  re-labels a trade (R7). Demo rows, unresolved and no-data signals are
  excluded from training.
- The model is L2 logistic regression on standardized features, implemented
  in numpy (no new dependency), small enough to stay honest at the current
  sample size (~dozens of rows). Reported skill is walk-forward only:
  an expanding training window that never sees the row it scores.
- Context features missing at decision time are imputed with the training
  window's median — imputation values are stored in the model file so
  training-time and scoring-time behavior cannot drift apart.
- A missing or unreadable model file degrades to "no score" (R4); callers
  disclose when scoring was expected but skipped.

Model file: `<data_dir>/learn_model.json`, fingerprinted like the ledger's
`weights_hash` so every `ml_score` can be traced to the model that made it.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from signaldesk import ledger as ledger_mod
from signaldesk import outcomes as outcomes_mod

MODEL_FILENAME = "learn_model.json"
DEFAULT_L2 = 1.0
DEFAULT_MIN_TRAIN = 20
_NEWTON_ITERS = 50

# Features frozen at decision time. Context keys come from the ledger's
# `context` dict (trial T1 enrichment); missing values become NaN here and
# are imputed with training medians, never guessed at scoring time.
CONTEXT_FEATURES = ("change_24h_pct", "volume_ratio", "rsi14",
                    "sma20_dist_pct", "sma50_dist_pct", "btc_mom_20d_pct")
FEATURE_NAMES = ("score", "risk_pct", "cost_in_r",
                 "entry_mode_market", "entry_mode_pullback",
                 *CONTEXT_FEATURES, "btc_mom_missing")

LABEL_DEFINITION = "r_net > 0 (triple-barrier, outcomes.py declared conventions)"


class LearningError(ValueError):
    """Training is impossible right now (no data, single class, ...)."""


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def feature_vector(record) -> dict[str, float]:
    """Raw decision-time features for one ledger record (NaN = missing)."""
    ctx = getattr(record, "context", None) or {}
    mode = getattr(record, "entry_mode", "daily")
    vec: dict[str, float] = {
        "score": float(record.score),
        "risk_pct": float(getattr(record, "risk_pct", 0.0) or 0.0),
        "cost_in_r": float(getattr(record, "cost_in_r", 0.0) or 0.0),
        "entry_mode_market": 1.0 if mode == "market" else 0.0,
        "entry_mode_pullback": 1.0 if mode == "pullback" else 0.0,
        # doubles as the crypto-market flag: btc_mom exists only there
        "btc_mom_missing": 0.0 if _finite(ctx.get("btc_mom_20d_pct")) else 1.0,
    }
    for key in CONTEXT_FEATURES:
        v = ctx.get(key)
        vec[key] = float(v) if _finite(v) else math.nan
    return vec


@dataclass
class LearningRow:
    """One training example: a resolved ledger signal with its label."""
    signal_id: str
    decision_date: str            # YYYY-MM-DD, the walk-forward sort key
    market: str
    symbol: str
    source: str
    r_net: float
    label: int                    # 1 if r_net > 0
    features: dict[str, float]


def build_dataset(records, resolutions: dict[str, dict]) -> list[LearningRow]:
    """Join ledger records with their latest resolution; chronological order.

    `resolutions` is the `outcomes.load_latest()` mapping. Demo rows, signals
    without a resolution and signals that resolved to open/no_data carry no
    label and are dropped.
    """
    rows: list[LearningRow] = []
    for rec in records:
        if getattr(rec, "demo", False):
            continue
        res = resolutions.get(rec.signal_id)
        if not res or res.get("status") in ("open", "no_data", None, ""):
            continue
        r_net = float(res.get("r_net", 0.0) or 0.0)
        rows.append(LearningRow(
            signal_id=rec.signal_id,
            decision_date=(getattr(rec, "bars_last_date", "")
                           or getattr(rec, "as_of", "")
                           or getattr(rec, "created_at", ""))[:10],
            market=rec.market, symbol=rec.symbol,
            source=getattr(rec, "source", ""),
            r_net=r_net, label=1 if r_net > 0 else 0,
            features=feature_vector(rec),
        ))
    rows.sort(key=lambda r: (r.decision_date, r.signal_id))
    return rows


def load_dataset(data_dir: Path) -> tuple[list[LearningRow], dict[str, int]]:
    """Training rows plus the ledger counts that explain what was excluded."""
    data_dir = Path(data_dir)
    records = ledger_mod.read_records(ledger_mod.ledger_path(data_dir))
    resolutions = outcomes_mod.load_latest(outcomes_mod.outcomes_path(data_dir))
    rows = build_dataset(records, resolutions)
    counts = {
        "ledger": len(records),
        "demo": sum(1 for r in records if getattr(r, "demo", False)),
        "labeled": len(rows),
        "excluded": len(records) - len(rows),
    }
    return rows, counts


# --------------------------------------------------------------------------
# model: L2 logistic regression, Newton-Raphson, numpy only
# --------------------------------------------------------------------------

def _sigmoid(z) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))


def _fit_logistic(X: np.ndarray, y: np.ndarray, l2: float) -> tuple[np.ndarray, float]:
    """Weights and intercept minimizing log-loss + (l2/2)||w||^2 (intercept free).

    Expects standardized columns — the L2 penalty is scale-sensitive.
    """
    Xb = np.hstack([X, np.ones((X.shape[0], 1))])
    pen = np.eye(Xb.shape[1]) * float(l2)
    pen[-1, -1] = 0.0
    w = np.zeros(Xb.shape[1])
    for _ in range(_NEWTON_ITERS):
        p = _sigmoid(Xb @ w)
        grad = Xb.T @ (p - y) + pen @ w
        s = np.clip(p * (1.0 - p), 1e-9, None)
        H = Xb.T @ (Xb * s[:, None]) + pen
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, grad, rcond=None)[0]
        w_new = w - step
        if float(np.max(np.abs(w_new - w))) < 1e-10:
            w = w_new
            break
        w = w_new
    return w[:-1], float(w[-1])


def _medians(rows: list[LearningRow]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for name in FEATURE_NAMES:
        vals = [r.features[name] for r in rows if math.isfinite(r.features[name])]
        out[name] = float(np.median(vals)) if vals else None
    return out


def _imputed_matrix(rows: list[LearningRow], imp: dict[str, float | None]) -> np.ndarray:
    cols = []
    for name in FEATURE_NAMES:
        fallback = imp[name] if imp[name] is not None else 0.0
        cols.append([r.features[name] if math.isfinite(r.features[name]) else fallback
                     for r in rows])
    return np.array(cols, dtype=float).T


def _standardized(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Z-score columns; zero-variance columns pass through (std := 1)."""
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    return (X - mu) / sd, mu, sd


def _make_model(imp: dict[str, float | None], mu: np.ndarray, sd: np.ndarray,
                w: np.ndarray, b: float) -> dict:
    """JSON-safe model payload; coefficients are fit on standardized columns."""
    return {
        "feature_names": list(FEATURE_NAMES),
        "imputation": dict(imp),
        "standardization": {name: {"mean": float(mu[j]), "std": float(sd[j])}
                            for j, name in enumerate(FEATURE_NAMES)},
        "coefficients": {name: float(w[j]) for j, name in enumerate(FEATURE_NAMES)},
        "intercept": float(b),
    }


def _predict(model: dict, features: dict[str, float]) -> float:
    z = float(model["intercept"])
    for name in model["feature_names"]:
        v = features.get(name)
        if v is None or not _finite(v):
            v = model["imputation"].get(name)
            if v is None:
                v = 0.0
        stats = model["standardization"][name]
        z += model["coefficients"][name] * ((float(v) - stats["mean"]) / stats["std"])
    return float(_sigmoid(np.array(z)))


def score_features(model: dict, features: dict[str, float]) -> float:
    """P(label = 1) for a raw feature dict (missing values imputed)."""
    return _predict(model, features)


def score_record(model: dict, record) -> float:
    """P(r_net > 0) for a ledger record under the given model."""
    return _predict(model, feature_vector(record))


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

def _auc(scored: list[dict]) -> float | None:
    """Mann-Whitney AUC with average ranks for ties; None if one class."""
    n_pos = sum(1 for d in scored if d["label"] == 1)
    n_neg = len(scored) - n_pos
    if not n_pos or not n_neg:
        return None
    order = sorted(range(len(scored)), key=lambda i: scored[i]["p"])
    ranks = [0.0] * len(scored)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scored[order[j + 1]]["p"] == scored[order[i]]["p"]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_sum_pos = sum(r for r, d in zip(ranks, scored) if d["label"] == 1)
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _log_loss(scored: list[dict], p_fixed: float | None = None) -> float:
    eps = 1e-6
    total = 0.0
    for d in scored:
        p = min(max(p_fixed if p_fixed is not None else d["p"], eps), 1.0 - eps)
        total -= d["label"] * math.log(p) + (1 - d["label"]) * math.log(1.0 - p)
    return total / len(scored) if scored else float("nan")


def _terciles(scored: list[dict]) -> list[dict]:
    """Expectancy by predicted-probability tercile — the monotonicity evidence."""
    ordered = sorted(scored, key=lambda d: d["p"])
    if not ordered:
        return []
    out = []
    for band, chunk in zip(("low", "mid", "high"), np.array_split(np.asarray(ordered, dtype=object), 3)):
        chunk = list(chunk)
        if not chunk:
            continue
        out.append({
            "band": band,
            "n": len(chunk),
            "wins": int(sum(1 for d in chunk if d["label"] == 1)),
            "expectancy_r": round(float(np.mean([d["r_net"] for d in chunk])), 4),
            "p_range": [round(float(min(d["p"] for d in chunk)), 4),
                        round(float(max(d["p"] for d in chunk)), 4)],
        })
    return out


# --------------------------------------------------------------------------
# train / persist / shadow scoring
# --------------------------------------------------------------------------

def _fingerprint(model: dict) -> str:
    basis = {k: v for k, v in model.items()
             if k not in ("trained_at", "fingerprint", "app_version", "walk_forward")}
    blob = json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def train(rows: list[LearningRow], *, min_train: int = DEFAULT_MIN_TRAIN,
          l2: float = DEFAULT_L2) -> dict:
    """Fit the meta-label model; report skill walk-forward, save the full fit.

    The walk-forward pass refits on an expanding window and scores only the
    next row (imputation medians and standardization recomputed per window,
    so nothing leaks). The returned model is finally fit on all rows — it is
    for scoring future signals, while `walk_forward` is the honest skill.
    """
    if not rows:
        raise LearningError("no resolved signals to train on")
    labels = {r.label for r in rows}
    if len(labels) < 2:
        cls = "wins" if 1 in labels else "losses"
        raise LearningError(f"only {cls} in the resolved sample — nothing to separate yet")

    positives = sum(1 for r in rows if r.label == 1)
    imp = _medians(rows)
    X, mu, sd = _standardized(_imputed_matrix(rows, imp))
    y = np.array([r.label for r in rows], dtype=float)
    w, b = _fit_logistic(X, y, l2)
    model = {
        "model_type": "logistic_l2",
        "l2": float(l2),
        "label_definition": LABEL_DEFINITION,
        "n_train": len(rows),
        "positives": positives,
        "base_rate": positives / len(rows),
        **_make_model(imp, mu, sd, w, b),
    }

    scored: list[dict] = []
    skipped = 0
    for i in range(max(int(min_train), 1), len(rows)):
        window = rows[:i]
        if len({r.label for r in window}) < 2:
            skipped += 1
            continue
        imp_i = _medians(window)
        Xw, mu_i, sd_i = _standardized(_imputed_matrix(window, imp_i))
        ww, bw = _fit_logistic(Xw, np.array([r.label for r in window], dtype=float), l2)
        step_model = _make_model(imp_i, mu_i, sd_i, ww, bw)
        p = _predict(step_model, rows[i].features)
        scored.append({"p": p, "label": rows[i].label, "r_net": rows[i].r_net})

    auc = _auc(scored) if scored else None
    model["walk_forward"] = {
        "min_train": int(min_train),
        "n_scored": len(scored),
        "windows_skipped_single_class": skipped,
        "auc": None if auc is None else round(float(auc), 4),
        "log_loss": round(_log_loss(scored), 4) if scored else None,
        "base_log_loss": round(_log_loss(scored, p_fixed=sum(
            d["label"] for d in scored) / len(scored)), 4) if scored else None,
        "terciles": _terciles(scored),
        "all_expectancy_r": round(float(np.mean([d["r_net"] for d in scored])), 4) if scored else None,
    }
    model["trained_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    import signaldesk
    model["app_version"] = getattr(signaldesk, "__version__", "")
    model["fingerprint"] = _fingerprint(model)
    return model


def model_path(data_dir: Path) -> Path:
    return Path(data_dir) / MODEL_FILENAME


def default_model_path(run_dir: Path) -> Path:
    """Mirror of `ledger.default_ledger_path`: runs/<id> -> <home> root."""
    run_dir = Path(run_dir)
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent / MODEL_FILENAME
    return run_dir / MODEL_FILENAME


def save_model(path: Path, model: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, indent=2), encoding="utf-8")
    return path


def load_model(path: Path) -> dict | None:
    """The saved model, or None when absent/unreadable (R4: degrade, disclose)."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(model, dict) or "coefficients" not in model:
        return None
    return model


def apply_shadow_scores(records, model: dict | None) -> tuple[int, str]:
    """Stamp `ml_score` / `ml_fingerprint` on ledger records, in place.

    Shadow only: nothing downstream may read ml_score to filter or size a
    signal until a pre-registered trial closes positive (R7). Records that
    fail to score are left untouched rather than guessed.
    """
    if not model:
        return 0, ""
    fp = str(model.get("fingerprint", ""))
    n = 0
    for rec in records:
        try:
            p = score_record(model, rec)
        except Exception:
            continue
        rec.ml_score = round(float(p), 6)
        rec.ml_fingerprint = fp
        n += 1
    return n, fp


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def shadow_report(data_dir: Path) -> dict:
    """What the saved model claims, measured against the ledger's history."""
    data_dir = Path(data_dir)
    model = load_model(model_path(data_dir))
    rows, counts = load_dataset(data_dir)
    records = ledger_mod.read_records(ledger_mod.ledger_path(data_dir))
    out: dict = {
        "model_path": str(model_path(data_dir)),
        "model": None if model is None else {
            "fingerprint": model.get("fingerprint"),
            "trained_at": model.get("trained_at"),
            "n_train": model.get("n_train"),
            "base_rate": model.get("base_rate"),
        },
        "ledger": counts,
        "n_ml_scored": sum(1 for r in records if getattr(r, "ml_score", None) is not None),
        "walk_forward": (model or {}).get("walk_forward"),
        "coefficients": (model or {}).get("coefficients"),
    }
    if model and rows:
        # in-sample on purpose: the final model saw these rows. The honest
        # numbers live in walk_forward; this only checks the score still
        # moves in the right direction across the whole history.
        hist = [{"p": _predict(model, r.features), "label": r.label, "r_net": r.r_net}
                for r in rows]
        out["history_terciles_in_sample"] = _terciles(hist)
        out["all_expectancy_r"] = round(float(np.mean([r.r_net for r in rows])), 4)
    return out


def render_train_text(model: dict) -> str:
    wf = model.get("walk_forward") or {}
    lines = [
        f"learn: meta-label model trained on {model['n_train']} resolved signals "
        f"({model['positives']} wins, base rate {model['base_rate']:.0%})",
        f"label: {model['label_definition']}",
    ]
    if wf.get("n_scored"):
        auc = wf.get("auc")
        auc_txt = f"{auc:.2f}" if auc is not None else "n/a (one class out-of-sample)"
        lines.append(
            f"walk-forward (out-of-sample, expanding window from {wf['min_train']}): "
            f"{wf['n_scored']} scored · AUC {auc_txt} (0.5 = coin flip) · "
            f"log-loss {wf['log_loss']:.3f} vs base {wf['base_log_loss']:.3f}")
        for band in wf.get("terciles", []):
            lines.append(f"  {band['band']:4} p∈[{band['p_range'][0]:.2f},{band['p_range'][1]:.2f}] "
                         f"n={band['n']:3} wins={band['wins']:3} "
                         f"expectancy {band['expectancy_r']:+.2f}R")
        lines.append(f"  all scored: {wf.get('all_expectancy_r', 0.0):+.2f}R")
    else:
        lines.append("walk-forward: too few rows to score out-of-sample "
                     f"(need more than min_train={wf.get('min_train')})")
    ranked = sorted(model["coefficients"].items(), key=lambda kv: -abs(kv[1]))
    lines.append("coefficients (standardized; positive = predicts a win):")
    lines.append("  " + " · ".join(f"{k} {v:+.2f}" for k, v in ranked))
    lines.append("shadow mode: the model gates nothing. Score scans with it, judge")
    lines.append("`signaldesk learn report`, and pre-register any filter as a trial (R7)")
    lines.append("before it is allowed to skip or size a single signal.")
    if model["n_train"] < 100:
        lines.append("caveat: small sample — every number above is noisy and directional only.")
    return "\n".join(lines)


def render_report_text(rep: dict) -> str:
    model = rep.get("model")
    counts = rep.get("ledger", {})
    if model is None:
        return (f"no trained model ({rep['model_path']})\n"
                f"ledger holds {counts.get('labeled', 0)} resolved signal(s) — "
                "run `signaldesk learn train` once that is enough")
    lines = [
        f"model {model.get('fingerprint')} trained {str(model.get('trained_at'))[:10]} "
        f"on {model.get('n_train')} signals (base rate {model.get('base_rate', 0):.0%})",
        f"ledger records carrying an ml_score so far: {rep.get('n_ml_scored', 0)}",
        "",
        "walk-forward skill (out-of-sample — the honest numbers):",
    ]
    wf = rep.get("walk_forward") or {}
    if wf.get("n_scored"):
        auc = wf.get("auc")
        auc_txt = f"{auc:.2f}" if auc is not None else "n/a"
        lines.append(f"  AUC {auc_txt} · log-loss {wf.get('log_loss'):.3f} vs base "
                     f"{wf.get('base_log_loss'):.3f} over {wf['n_scored']} scored signals")
        for band in wf.get("terciles", []):
            lines.append(f"  {band['band']:4} p∈[{band['p_range'][0]:.2f},{band['p_range'][1]:.2f}] "
                         f"n={band['n']:3} expectancy {band['expectancy_r']:+.2f}R")
    else:
        lines.append("  not yet scored out-of-sample (retrain as outcomes accumulate)")
    if rep.get("history_terciles_in_sample"):
        lines.append("")
        lines.append("history by score tercile (IN-SAMPLE, indicative only):")
        for band in rep["history_terciles_in_sample"]:
            lines.append(f"  {band['band']:4} n={band['n']:3} wins={band['wins']:3} "
                         f"expectancy {band['expectancy_r']:+.2f}R")
        lines.append(f"  all: {rep.get('all_expectancy_r', 0.0):+.2f}R")
    lines.append("")
    lines.append("the score gates nothing on its own — any filter or sizing rule")
    lines.append("must be pre-registered as a trial and win out-of-sample first (R7).")
    return "\n".join(lines)
