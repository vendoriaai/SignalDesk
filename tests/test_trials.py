"""Trial log (item 33): pre-registration of rule changes, frozen criteria."""
from typer.testing import CliRunner

from signaldesk import trials

runner = CliRunner()


def test_log_trial_freezes_criteria_and_increments_ids(tmp_path):
    t1 = trials.log_trial(tmp_path, name="dedupe emission",
                          hypothesis="one bet per symbol-day stops correlated duplicates",
                          change="scan appends with dedupe=True",
                          judging="expectancy in R net of cost vs -0.11R baseline", min_weeks=4)
    t2 = trials.log_trial(tmp_path, name="second", hypothesis="h", change="c",
                          judging="j", min_signals=10)
    assert t1["id"] == "T1" and t2["id"] == "T2"
    assert t1["status"] == "running" and t1["judging"].startswith("expectancy")
    stored = trials.read_trials(tmp_path)
    assert len(stored) == 2 and stored[0]["name"] == "dedupe emission"


def test_close_trial_records_outcome_without_editing_declaration(tmp_path):
    trial = trials.log_trial(tmp_path, name="n", hypothesis="h", change="c", judging="j")
    closed = trials.close_trial(tmp_path, trial["id"], "confirmed: expectancy +0.1R")
    assert closed["status"] == "closed" and closed["outcome"].startswith("confirmed")
    stored = trials.read_trials(tmp_path)[0]
    assert stored["hypothesis"] == "h" and stored["judging"] == "j"   # untouched
    assert trials.close_trial(tmp_path, "T99", "nope") is None


def test_render_text_lists_trials(tmp_path):
    trials.log_trial(tmp_path, name="dedupe", hypothesis="h", change="c", judging="j")
    text = trials.render_text(trials.read_trials(tmp_path))
    assert "T1 [running]" in text and "dedupe" in text
    assert trials.render_text([]).startswith("no trials logged")


def test_trial_cli_add_list_close(tmp_path, monkeypatch):
    from signaldesk.cli import app

    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["trial", "add", "--name", "T", "--hypothesis", "H",
                                 "--change", "C", "--judging", "J", "--min-signals", "5"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["trial", "list"])
    assert result.exit_code == 0 and "T1 [running]" in result.output
    result = runner.invoke(app, ["trial", "close", "T1", "--outcome", "done"])
    assert result.exit_code == 0, result.output
    assert trials.read_trials(tmp_path)[0]["status"] == "closed"
