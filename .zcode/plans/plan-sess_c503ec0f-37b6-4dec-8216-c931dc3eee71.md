# Multi-timeframe entry refinement (30m / 15m / 5m / 1m)

**Goal:** after the daily-timeframe scan ranks candidates (scoring preset `trend-momentum-v1` stays untouched), a new **Phase 7.5 "entry refinement"** pulls intraday bars for just the final top signals and refines entry/stop/TP so entries are better timed and 2R/3R targets sit closer (tighter structural risk → higher hit odds). Applies to WF-1/WF-3/WF-4 (all go through `run_market_scan`) and WF-2 deep dive. Read-only, all new numbers cited (R1), missing timeframes degrade with disclosure (R4).

## Entry rules (deterministic, pure code — no LLM)

Per-TF roles: **30m** = structure (stop), **15m** = bias + pullback zone, **5m** = trigger, **1m** = micro-timing. Missing TF → use next-best available; none → keep daily plan + disclosure.

- 15m bias bullish = close > EMA21 and (MACD hist > 0 or rising)
- **wait** — 15m bias bearish: keep daily plan numbers, note "wait for 15m close back above EMA21"
- **pullback** — 15m bullish but overextended (RSI15m ≥ 68 or RSI5m ≥ 72): entry zone = 15m EMA21 ± 0.25×ATR15m; stop = tighter of (entry − 1.5×ATR15m, 15m swing low − 0.25×ATR15m), floored at entry − 0.5×ATR15m
- **market** — 15m bullish, not overextended: entry = daily entry; refined stop = tighter of (daily stop, entry − 1.5×ATR30m, 30m swing low − 0.25×ATR30m), same floor
- TP1 = entry + 2×refined risk, TP2 = +3×; invariant stop < entry < tp1 < tp2 (test-pinned)

Per-TF periods (within yfinance caps): 30m→30d, 15m→14d, 5m→5d, 1m→2d. Runtime: ≤5 signals × 4 TFs = ≤20 extra fetches (~15–40 s) + one extra sandbox pass — 4-minute budget preserved.

## Changes

**New files**
- `signaldesk/workflows/entry_refine.py` — shared helper (market_scan + deep_dive): fetch entry TFs (non-fatal per TF), second sandbox pass computing per-symbol×TF features (reuses `compute_features`), call the new scoring function, register citations, render entry chart, emit `P7.5` events + CHART events.
- `tests/test_entry_refinement.py`

**Changed**
- `signaldesk/markets.py` — `DEFAULT_ENTRY_TIMEFRAMES = ("30m","15m","5m","1m")` + period map.
- `signaldesk/strategy/scoring.py` — `EntryPlan` dataclass + pure `refine_entry_plan(daily_plan, ltf_features)`. No weight/preset changes.
- `signaldesk/analysis/charts.py` — `render_entry_chart()`: price + EMA21 with horizontal entry/stop/TP1/TP2 lines, same dark theme.
- `signaldesk/tools/demo.py` — extend `demo_intraday` (KeyError today for 30m/5m/1m): bars_per_day 30m=48, 5m=288, 1m=1440; demo days 30/14/5/2 (~1.3–2.9k bars/symbol).
- `signaldesk/workflows/market_scan.py` — `MarketScanRequest.entry_timeframes` field; Phase 7.5 between `signals = signals[:5]` (:404) and Phase 8 (:406); attach `Signal.entry_plan`, confluence notes, chart key `{sym}-15m-entry` (gallery picks it up automatically).
- `signaldesk/workflows/deep_dive.py` — same refinement for its single signal.
- `signaldesk/report/schema.py` — optional `EntryPlan` model + `Signal.entry_plan: EntryPlan | None = None` (optional → eval-safe).
- `signaldesk/report/render.py` — per-signal "entry plan" block under the signals table; Markdown renders in the UI as-is.
- `signaldesk/userconfig.py` — `Settings.DEFAULTS["entry_refinement"] = True` (off-switch).
- `signaldesk/cli.py` — `scan --entry-tf` (comma list, "off" disables); `ask` path threads it too.
- `signaldesk/api.py` — `ScanRequestIn.entry_timeframes`; `_launch_scan` honors the settings toggle; `SettingsIn` + setter gain it.
- `ui/src/SettingsView.jsx` — "Intraday entry refinement" checkbox.

**Intentionally unchanged:** `store.py` SignalRow (entry plan travels in report.json — no migration), scoring weights, existing 1d/1h confluence, `DEFAULT_TIMEFRAMES`.

## Citations (R1)

Per refined signal: direct cites for each LTF close used (`ohlcv_SYM_TF.csv`, row_key=last bar); derived cites for features (`formula="EMA21(close,15m)"` etc.); derived cites for the four plan numbers with `derived_from`. All appended to `Signal.citations` → coverage stays 1.0, critic + evals stay green.

## Tests & verification

- Unit: `refine_entry_plan` modes (market/pullback/wait), tighter-stop + floor invariants (in `tests/test_scoring.py` style).
- E2E demo scan: every signal has a valid entry_plan, geometry holds, coverage 1.0, entry chart file + chart key exist; disclosure path when a TF fetch fails; `--entry-tf off` yields no entry_plan.
- Run `pytest` (69 existing stay green), `python -m signaldesk.evals.runner` (30/30), `ruff check .`, `cd ui && npm run build`.
- Verify visually with `signaldesk scan crypto --demo` into a temp `SIGNALDESK_HOME`; the running server at 127.0.0.1:8787 will need a restart to pick up the new code.

## Docs / DOX pass

`workflows.md` (Phase 7.5 spec + WF-2 note), `README.md` (production steps + status), `docs/strategies.md` (entry-refinement section), `docs/data-sources.md` (intraday limits), `CHANGELOG.md` ([Unreleased]), `AGENTS.md` Child DOX Index (new `entry_refine.py` entry). Root rules preserved: R1/R4/R5, no prediction wording, read-only tools.