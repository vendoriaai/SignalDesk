import json

from typer.testing import CliRunner

from signaldesk.cli import app

runner = CliRunner()


def _extract_json(stdout: str) -> dict:
    start = stdout.index("{")
    depth = 0
    for i, ch in enumerate(stdout[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(stdout[start : i + 1])
    raise AssertionError("no JSON object in output")


def test_scan_demo_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["scan", "crypto", "--demo", "--json"])
    assert result.exit_code == 0, result.output
    report = _extract_json(result.stdout)
    assert report["market"] == "crypto"
    assert report["signals"] or report["avoid"]
    assert report["disclaimer"].startswith("Not financial advice")


def test_scan_demo_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["scan", "crypto", "--demo"])
    assert result.exit_code == 0
    assert "Ranked signals" in result.output
    assert "Citations" in result.output
    assert "Entry plans (intraday refinement)" in result.output


def test_scan_unknown_market_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["scan", "commodities", "--demo"])
    assert result.exit_code != 0
    assert "unknown market" in result.output or "scan failed" in result.output


def test_scan_forex_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["scan", "forex", "--demo"])
    assert result.exit_code == 0, result.output
    assert "Session note" in result.output
    assert "fx-momentum-v1" in result.output


def test_scan_demo_entry_plans_on_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["scan", "crypto", "--demo", "--json"])
    assert result.exit_code == 0, result.output
    report = _extract_json(result.stdout)
    for s in report["signals"]:
        assert s["entry_plan"] is not None
        assert s["entry_plan"]["mode"] in ("market", "pullback", "wait")


def test_scan_demo_entry_tf_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["scan", "crypto", "--demo", "--json", "--entry-tf", "off"])
    assert result.exit_code == 0, result.output
    report = _extract_json(result.stdout)
    assert all(s["entry_plan"] is None for s in report["signals"])


def test_deepdive_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["deepdive", "AAPL", "--demo"])
    assert result.exit_code == 0, result.output
    assert "Deep Dive: AAPL" in result.output
    assert "EDGAR" in result.output


# --- WF-5: outcomes pipeline + paper execution log ---------------------------

def _seed_ledger(tmp_path):
    from signaldesk import ledger as ledger_mod

    records = [
        ledger_mod.LedgerRecord(
            signal_id="run1:BTCUSD", created_at="2026-09-02T00:00:00+00:00",
            run_id="run1", market="crypto", symbol="BTCUSD", score=80.0,
            entry=100.0, stop=95.0, tp1=110.0, tp2=115.0, risk=5.0, risk_pct=5.0,
            cost_pct=0.25, cost_in_r=0.05, bars_last_date="2026-06-01"),
        ledger_mod.LedgerRecord(
            signal_id="run1:ETHUSD", created_at="2026-09-02T00:00:00+00:00",
            run_id="run1", market="crypto", symbol="ETHUSD", score=75.0,
            entry=200.0, stop=190.0, tp1=220.0, tp2=230.0, risk=10.0, risk_pct=5.0,
            cost_pct=0.25, cost_in_r=0.025, bars_last_date="2026-06-01"),
    ]
    ledger_mod.append_records(ledger_mod.ledger_path(tmp_path), records)
    return records


def test_outcomes_cli_demo_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["outcomes"])
    assert result.exit_code != 0                      # nothing scored yet
    assert "no scored signals" in result.output

    _seed_ledger(tmp_path)
    result = runner.invoke(app, ["outcomes", "--demo"])
    assert result.exit_code == 0, result.output
    assert "expectancy" in result.output
    assert "signals: 2 recorded" in result.output


def test_paper_cli_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    records = _seed_ledger(tmp_path)

    result = runner.invoke(app, ["paper", "fill", "nope:BTCUSD", "--price", "101"])
    assert result.exit_code == 1
    assert "not in ledger" in result.output

    result = runner.invoke(app, ["paper", "fill", records[0].signal_id, "--price", "101"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["paper", "miss", records[1].signal_id])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["paper", "list"])
    assert result.exit_code == 0, result.output
    assert "fill BTCUSD" in result.output
    assert "miss ETHUSD" in result.output
    assert "gap -0.20R" in result.output              # filled 101 vs entry 100, R=5
    assert "fill rate 50%" in result.output


def test_paper_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    result = runner.invoke(app, ["paper", "list"])
    assert result.exit_code == 0, result.output
    assert "no paper events" in result.output
