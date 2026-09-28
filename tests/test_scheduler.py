"""Daily resolution scheduler: due logic, retry-on-error, state file."""
from datetime import date, datetime

from signaldesk import scheduler


def test_due_is_per_local_day():
    assert scheduler.due({}, date(2026, 9, 28))
    assert not scheduler.due({"last_resolution_date": "2026-09-28"}, date(2026, 9, 28))
    assert scheduler.due({"last_resolution_date": "2026-09-28"}, date(2026, 9, 29))


def test_success_marks_the_day_and_skips_until_tomorrow(tmp_path):
    first = scheduler.run_if_due(tmp_path, now=datetime(2026, 9, 28, 9, 0))
    assert first["last_status"] == "ok"
    assert first["last_resolution_date"] == "2026-09-28"
    assert first["last_counts"] == {"rows": 0}          # empty ledger: nothing to resolve

    assert scheduler.run_if_due(tmp_path, now=datetime(2026, 9, 28, 15, 0)) is None
    again = scheduler.run_if_due(tmp_path, now=datetime(2026, 9, 29, 9, 0))
    assert again["last_resolution_date"] == "2026-09-29"


def test_error_records_failure_and_retries_same_day(tmp_path, monkeypatch):
    def boom(_data_dir):
        raise RuntimeError("offline")

    monkeypatch.setattr(scheduler.resolver_mod, "run_resolution", boom)
    state = scheduler.run_if_due(tmp_path, now=datetime(2026, 9, 28, 9, 0))
    assert state["last_status"] == "error"
    assert "offline" in state["last_error"]
    # the day is NOT marked done: the next hourly check retries
    assert scheduler.due(scheduler.load_state(tmp_path), date(2026, 9, 28))

    monkeypatch.setattr(scheduler.resolver_mod, "run_resolution", lambda _d: [])
    recovered = scheduler.run_if_due(tmp_path, now=datetime(2026, 9, 28, 10, 0))
    assert recovered["last_status"] == "ok"
    assert recovered["last_resolution_date"] == "2026-09-28"


def test_state_file_roundtrip_and_garbage(tmp_path):
    assert scheduler.load_state(tmp_path) == {}
    scheduler.save_state(tmp_path, {"last_status": "ok"})
    assert scheduler.load_state(tmp_path) == {"last_status": "ok"}
    (tmp_path / "outcomes_state.json").write_text("{not json", encoding="utf-8")
    assert scheduler.load_state(tmp_path) == {}
