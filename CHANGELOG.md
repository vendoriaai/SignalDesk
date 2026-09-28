# Changelog

All notable changes to SignalDesk are documented here.

## [Unreleased]

### Added
- **WF-5 remainder (roadmap item 28) — outcomes in the app, on a schedule:**
  - Scheduled daily resolution: a server background loop (`scheduler.py`)
    runs one resolution pass per local day — immediately on startup when
    today's pass hasn't happened yet, hourly retry after a failure (state in
    `outcomes_state.json`; disable with `SIGNALDESK_SCHEDULER=0`).
  - Outcomes dashboard in the UI: expectancy in R with CI, hit rate, profit
    factor, cost drag, exit-policy variants, breakdowns by market/entry mode,
    and the per-signal ledger table. `GET /api/outcomes` serves the cached
    resolutions (never fetches bars); `POST /api/outcomes/resolve` or the
    dashboard's "Resolve now" button triggers a fresh pass.
  - Paper execution log (`paper.py`, append-only `paper.jsonl`): mark each
    signal filled (with the real price) or missed — from the dashboard row or
    `signaldesk paper fill|miss|list`. Reports fill rate, direction-aware
    entry slippage in R, and crypto funding drag over resolved holds.
    Outcomes keep resolving the modelled plan (rule R7): paper events are
    additive measurement of the execution-vs-model gap, and the first
    fill/miss decision per signal is the one that counts.
  - Shared resolution pipeline (`resolver.py`): the CLI `outcomes` command,
    the API resolve endpoint and the scheduler now run one code path
    (ledger filters → per-market bars → triple-barrier → `outcomes.jsonl`).
- **P0 measurement layer** — you cannot improve what you do not measure:
  - Signal ledger (`signaldesk/ledger.py`, `~/.signaldesk/signals.jsonl`):
    every emitted signal recorded append-only with actionable levels + entry
    mode, risk unit, cost-in-R, sha256 of the OHLCV snapshot it came from, the
    decision bar date, a scoring-weights fingerprint, and a demo flag.
  - Triple-barrier outcome resolver (`signaldesk/outcomes.py`): TP1/TP2/stop/time
    barrier with next-bar start, pessimistic same-bar tie-break, MFE/MAE, and
    three pre-computed R variants so exit policies can be re-scored later.
  - Outcome statistics (`signaldesk/metrics.py`): expectancy in R net of cost
    with a week-block bootstrap CI, Wilson hit-rate interval, profit factor,
    median bars-to-TP1, breakdowns by mode/market/symbol, and explicit
    small-sample caveats.
  - CLI: `signaldesk outcomes [--backfill] [--horizon N] [--market M] [--json]`
    and `signaldesk ledger`.
- **Cost model** (`signaldesk/costs.py`): per-market/per-symbol round-trip cost
  assumptions (fees + spread + slippage), `cost_in_r` and `breakeven_win_rate`.
  The assumed cost is cited in reports (`source_tool="cost_model"`) and shown
  per signal with the break-even hit rate at TP1.
- **New "Risk units & costs" report section** plus cost-in-R on every entry
  plan; a disclosure states the assumed costs for the run.

### Changed
- **Risk-unit floor (rule R6).** Stops can no longer sit closer than
  max(0.75×ATR(14, daily), 15× the assumed round-trip cost). Previously the
  intraday refinement rebuilt the stop from the 20-bar intraday swing low —
  ~10 hours on 30m, not the ~1 month the same lookback means on daily bars —
  which collapsed R from ~5% of price to ~0.16% on ETH and put round-trip
  costs at 0.8–3R (a 2R target would have needed a 61–75%+ win rate to break
  even). Refinement may now tighten R by at most ~25%; `floored` plans are
  labelled in the report and their stop citation states the floor.
- Intraday structure stops are ATR-only (1.5×ATR of the structure timeframe);
  the intraday swing low remains in the cited snapshot as context.
- Deep dives get the same cost floor, cost citations and ledger recording.

### Fixed
- Sandbox `ModuleNotFoundError: No module named 'signaldesk'`: the server
  imported the package via cwd shadowing, but sandbox children run from the
  run directory and relied on site-packages — where a stale editable install
  still pointed at the pre-move checkout (`Downloads\Signal Desk pro`).
  Re-install with `pip install -e .` after moving the checkout; the executor
  now also pins the child's PYTHONPATH to the package this process actually
  loaded, so a stale install cannot break the sandbox again.
- yfinance resilience: downloads now pass `timeout=20` (the 10 s default was
  expiring on slow DNS — the `curl: (28) Resolving timed out` / bogus
  "possibly delisted" noise) and retry with 1.5 s/4.0 s backoff (was a single
  1.5 s retry), riding out brief Yahoo 429-throttling/DNS blips before a
  symbol is disclosed as failed.
- yfinance normalization: an empty or unnamed-index response used to raise
  `KeyError: 'date'` and silently drop the symbol (BTCUSD and BNBUSD vanished
  from a live scan). `_normalize_yf` now returns an empty frame, the OHLCV tool
  retries once and raises a clear message, and the quotes batch retries when
  every symbol comes back empty.
- Entry-plan markdown now reports "risk N (x% tighter/wider than daily)"
  instead of an ambiguous signed percentage.
- Sandbox-dependent tests: the editable install pointed at the pre-rename
  folder, so `import signaldesk` failed in the sandbox children; documented the
  `pip install -e .` remedy in the README.

### Earlier since v1.0.0 (already shipped)
- Intraday entry refinement (Phase 7.5) — top signals re-checked on
  30m/15m/5m/1m bars: enter now vs. pullback (15m EMA21 zone) vs. wait mode,
  per-signal 15m entry chart in the "Charts analyzed" gallery, every level
  cited. Off-switches: Settings toggle, `--entry-tf off`, `entry_timeframes: []`
  in the API (custom sets via `--entry-tf 30m,15m`). Settings key
  `entry_refinement` (default on).
- OpenRouter BYOK + model override; chart analysis in chat.
- Main scan sandbox pass timeout raised 60s → 180s (chart rendering for the
  full universe legitimately exceeds 60s on slower machines).
- History view parity with the chat view: the analysis trace now replays for
  past runs even after a server restart (`GET /api/runs/{id}` falls back to the
  persisted `trace_jsonl` when the in-memory buffer is empty) and the
  "Charts analyzed" gallery renders there too.
- Responsive layout for narrow windows: below 980px the history list stacks
  above the detail pane (clamped height, scrollable grid), report tables scroll
  horizontally instead of crushing, chart cards shrink to 220px minimum; below
  720px the sidebar collapses to a top bar and settings rows stack.

## [1.0.0] — 2026-09-24

First public release.

### The agent
- Four workflows: crypto scan (WF-1), forex/metals scan with 20%-macro
  weighting (WF-3), ticker deep dive with EDGAR fundamentals (WF-2),
  watchlist scans with what-changed diffs (WF-4)
- Natural-language planner (deterministic rules first, LLM BYOK refinement)
- Universal citation system: every figure is a direct (tool → CSV → row) or
  derived (formula + inputs) citation; citation coverage is verified per run
- Key-free data stack: yfinance, CoinGecko, alternative.me, SEC EDGAR,
  DuckDuckGo fallback search; optional Tavily / FRED / Alpha Vantage keys
- Local-first: SQLite run history, OS keychain for secrets, offline browsing
  of past reports

### Desktop app
- FastAPI + React UI (chat, live execution trace, history, watchlists,
  settings), webview shell via pywebview
- Onboarding wizard: pick LLM → optional Supabase → consent → demo scan
- Update-available banner (manual download; TUF auto-update in 1.1)
- Consent-gated Supabase sync with GDPR "delete my data"

### Installers
- Windows: NSIS `.exe`, macOS: `.dmg` (aarch64), Linux: AppImage + `.deb`
  — all built by GitHub Actions on every `v*` tag

### Compliance
- Read-only tools; write-side adapters rejected at registration
- "Not financial advice" on every report, no promises of returns
