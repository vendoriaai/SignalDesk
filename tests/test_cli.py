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
    assert "Risk units & costs" in result.output


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
    assert report["signals"]
    for s in report["signals"]:
        # the bear demo emits SHORTs: they keep their daily plan (7.5 is
        # long-only, the 7.6 AI read is offline) — LONGs must carry a plan
        if s["direction"] == "SHORT":
            assert s["entry_plan"] is None
        else:
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


def _seed_learning_sample(tmp_path, n=12):
    """Ledger + outcomes with a deterministic rsi->outcome pattern for learning tests."""
    import json as _json

    from signaldesk import ledger as ledger_mod

    recs, lines = [], []
    for i in range(n):
        win = i % 3 != 0
        rsi = 40 + (i % 5) if win else 62 - (i % 5)
        rec = ledger_mod.LedgerRecord(
            signal_id=f"run{i % 2}:S{i:02d}", created_at=f"2026-07-{i + 1:02d}T00:00:00+00:00",
            run_id=f"run{i % 2}", market="crypto", symbol=f"S{i:02d}", score=75.0,
            entry=100.0, stop=95.0, tp1=110.0, tp2=115.0, risk=5.0, risk_pct=5.0,
            cost_pct=0.25, cost_in_r=0.05, bars_last_date=f"2026-07-{i + 1:02d}",
            context={"rsi14": float(rsi), "volume_ratio": 1.2, "sma20_dist_pct": 2.0,
                     "sma50_dist_pct": 5.0, "change_24h_pct": 1.0,
                     "btc_mom_20d_pct": 3.0, "btc_mom_missing": 0.0})
        recs.append(rec)
        lines.append(_json.dumps({
            "signal_id": rec.signal_id, "status": "tp2" if win else "sl",
            "r_net": 1.5 if win else -1.0,
            "resolved_at": f"2026-08-{i + 1:02d}T00:00:00+00:00"}))
    ledger_mod.append_records(ledger_mod.ledger_path(tmp_path), recs)
    (tmp_path / "outcomes.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return recs


def test_learn_train_refuses_tiny_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    _seed_ledger(tmp_path)
    result = runner.invoke(app, ["learn", "train"])
    assert result.exit_code == 1
    assert "resolved signal" in result.output


def test_learn_train_report_and_scan_shadow_scores(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALDESK_HOME", str(tmp_path))
    _seed_learning_sample(tmp_path)

    result = runner.invoke(app, ["learn", "train", "--min-signals", "8", "--min-train", "5"])
    assert result.exit_code == 0, result.output
    assert "walk-forward" in result.output
    assert (tmp_path / "learn_model.json").is_file()

    result = runner.invoke(app, ["learn", "report"])
    assert result.exit_code == 0, result.output
    assert "walk-forward" in result.output

    # the next scan stamps shadow scores on its new ledger records — and gates nothing
    from signaldesk import ledger as ledger_mod

    before = {r.signal_id for r in ledger_mod.read_records(ledger_mod.ledger_path(tmp_path))}
    result = runner.invoke(app, ["scan", "crypto", "--demo"])
    assert result.exit_code == 0, result.output
    fresh = [r for r in ledger_mod.read_records(ledger_mod.ledger_path(tmp_path))
             if r.signal_id not in before]
    assert fresh
    assert all(r.ml_score is not None and 0.0 <= r.ml_score <= 1.0 for r in fresh)
    assert all(r.ml_fingerprint for r in fresh)
