"""Trial log (roadmap item 33, foundation): pre-registration for rule changes.

Every change to signal generation — presets, entry policy, filters, sizing —
is declared here BEFORE it starts: name, hypothesis, what changed, the judging
criteria, and the minimum sample it must run for. Results are then judged on
the outcome layer (`signaldesk outcomes`) against those criteria, never
re-labelled after seeing numbers (rule R7).

`trials.jsonl` is append-only; `status` stays "running" until the operator
closes it (`signaldesk trial close`). With fewer than ~20 logged trials, the
deflated-Sharpe machinery is unnecessary; the log's job at this stage is to
make the count and the pre-committed criteria visible.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

TRIALS_FILENAME = "trials.jsonl"


def trials_path(data_dir: Path) -> Path:
    return Path(data_dir) / TRIALS_FILENAME


def read_trials(data_dir: Path) -> list[dict]:
    path = trials_path(data_dir)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def log_trial(data_dir: Path, *, name: str, hypothesis: str, change: str,
              judging: str, min_signals: int = 30, min_weeks: int = 4,
              started: str | None = None) -> dict:
    """Pre-register one change. The criteria are frozen at declaration time."""
    trial = {
        "id": f"T{len(read_trials(data_dir)) + 1}",
        "name": name,
        "hypothesis": hypothesis,
        "change": change,
        "judging": judging,
        "min_signals": min_signals,
        "min_weeks": min_weeks,
        "status": "running",
        "started": started or datetime.now(UTC).isoformat(timespec="seconds"),
        "closed": "",
        "outcome": "",
    }
    path = trials_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trial) + "\n")
    return trial


def close_trial(data_dir: Path, trial_id: str, outcome: str) -> dict | None:
    """Record the pre-committed judgement. The trial text is never edited."""
    trials = read_trials(data_dir)
    target = next((t for t in trials if t.get("id") == trial_id), None)
    if target is None:
        return None
    target.update({"status": "closed",
                   "closed": datetime.now(UTC).isoformat(timespec="seconds"),
                   "outcome": outcome})
    path = trials_path(data_dir)
    with path.open("w", encoding="utf-8") as fh:
        for t in trials:
            fh.write(json.dumps(t) + "\n")
    return target


def render_text(trials: list[dict]) -> str:
    lines: list[str] = []
    for t in trials:
        lines.append(f"{t.get('id')} [{t.get('status')}] {t.get('name')} "
                     f"(started {str(t.get('started'))[:10]})")
        lines.append(f"  hypothesis: {t.get('hypothesis')}")
        lines.append(f"  change:     {t.get('change')}")
        lines.append(f"  judging:    {t.get('judging')}")
        lines.append(f"  minimum:    {t.get('min_signals')} signals / {t.get('min_weeks')} weeks")
        if t.get("outcome"):
            lines.append(f"  outcome:    {t.get('outcome')} (closed {str(t.get('closed'))[:10]})")
        lines.append("")
    if not lines:
        lines.append("no trials logged — pre-register the next change with `signaldesk trial add`")
    return "\n".join(lines).rstrip()
