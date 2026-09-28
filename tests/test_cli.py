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
