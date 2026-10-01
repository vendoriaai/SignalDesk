# Changelog

All notable changes to SignalDesk are documented here.

## [Unreleased]

### Added
- **The AI now decides on everything the pipeline gathered (prompt era
  `ai-vision-v2`):** every Phase 6.5 signal-generation call receives, besides
  its own charts and indicator snapshot, the deterministic policy gates for
  both directions as advisory notes, the round-trip cost economics in R terms
  at the risk floor (cost-in-R and the break-even win rate for a 2R target),
  the exact last 10 daily OHLCV bars, a relative-strength line for every other
  scanned symbol, every collected market-news claim (article text, not a
  truncated join) and the macro snapshot (DXY/US10Y for forex/metals). The
  Phase 7.6 entry read gets the same market context alongside its chart
  ladder. The AI weights hash moves to the new prompt version so AI-era
  outcomes stay separable (R7); the trial's criteria are unchanged.
- **AI signal generation (Phase 6.5) — the vision LLM now generates the ranked
  signals themselves, from the data and the charts:** one vision call per
  scanned symbol (daily + 1h TA charts plus a brief of features, sentiment,
  BTC regime and market-context news) returns direction (LONG/SHORT/NONE), a
  0-100 conviction score, a one-line rationale and an optional invalidation
  level. The AI's picks are the signals (top 5 by score at or above the scan
  threshold); the report and ledger mark the provenance (`ai-vision-v1`,
  `generator="ai_vision_v1"`, an AI weights hash so outcomes stay separable
  per R7), and the first real application pre-registers trial
  `ai-signal-generation-v1` with frozen criteria. **Policy gates become
  advisory warnings on this path** (operator choice, disclosed per signal as
  "⚠ policy gate: … — AI proceeded"): a LONG in a bear regime or an
  overextended chase is emitted with its warning instead of being refused.
  Draft stops still clamp to [max(0.75×ATR(1d), 15× cost), 3×ATR(1d)] and TPs
  stay 2R/3R (R6); Phase 7.5/7.6 refinement and the AI entry read run on the
  AI signals exactly as before. Symbols the AI passed land in the avoid list
  with its rationale. Demo scans, no-key runs and total AI failure fall back
  to the deterministic engine verbatim, disclosed (R4). Each decision streams
  into the trace as a 🧠 P6.5 event before the signals appear. Settings
  toggle `ai_signal_generation` (default on).
- **AI chart read (Phase 7.6) — the trace now shows every chosen pair's charts
  on every timeframe before the signals, and a vision LLM picks the entry:**
  after the deterministic refinement, each signal (LONG and SHORT) gets its
  full chart ladder rendered and streamed into the execution trace — the 1d
  and 1h TA charts plus entry-style charts for 30m/15m/5m/1m — and one
  vision-capable LLM call per signal (LiteLLM, the configured
  provider/model) reads the ladder, daily → 1m, returning per-timeframe
  trend reads, the chosen entry, an optional stop, a rationale and a
  confidence. The read streams as 🧠 `analysis` trace events (chart events
  render inline in the UI) before the report emits the signals. The AI
  chooses only the entry level: the stop distance stays clamped to
  [max(0.75×ATR(1d), 15× cost), 3×ATR(1d)] and TPs stay 2R/3R (R6) — an
  implausible entry (> 3×ATR(1d) from the draft) falls back to the
  deterministic plan, disclosed (R4). Every plan number is cited (R1); the
  ledger records `entry_mode="ai_chart_v1"` and the first real application
  pre-registers trial `ai-chart-entry-v1` with frozen criteria (min 30
  resolved signals, judged on the expectancy CI) per R7. Settings toggle
  `ai_chart_entry` (default on); demo scans and missing LLM keys degrade to
  the deterministic plans with a disclosure. Trace chart events now render
  inline in the UI (alongside the new 🧠 analysis events), and the Charts
  analyzed gallery carries the full per-pair ladder (1d/1h/30m/15m/5m/1m).
- **Dual-direction engine (roadmap item 39) — the scan can now emit SHORT
  signals, not only LONGs:** every symbol is scored on both sides with a
  declared mirror preset (`trend-momentum-short-v1`: same weights, bearish
  conditions — below SMAs, MACD fading, RSI 30–50 band, fear-side sentiment)
  and the better eligible side is emitted, one signal per symbol. Shorts carry
  mirrored trade plans (stop above entry from 1.5×ATR / 20d swing high, TP1/TP2
  2R/3R below entry, same cost-aware risk floor), mirrored regime gates
  (refused above the 200d SMA, into a falling-knife 7d move < −50%, extended
  > 25% below SMA20, at the same 150% vol extreme; BTC above its own 200d SMA
  caps the whole crypto short book — the mirror of the item-32 long gate), and
  the same R3 guard mirrored (RSI ≤ 25 falling knife is never a SHORT). The
  measurement layer is direction-aware end to end: the triple-barrier resolver
  mirrors the barriers for shorts (same pessimistic stop-wins-tie rule, MFE/MAE
  signed favorable-vs-adverse), the ledger risk unit is |entry − stop|, and
  paper fill/miss slippage was already direction-aware. Deterministic intraday
  entry refinement (Phase 7.5) stays long-only — SHORT signals keep their
  daily plan unless the Phase 7.6 AI chart read (below) chooses their entry,
  and the report discloses which path ran (R4). The weights fingerprint
  changed (new short-side constants), so post-change ledger rows are traceable
  as a distinct rule set for A/B against the long-only era. Refused sides land
  in the avoid list cited and prefixed (`LONG refused: …`, `SHORT refused: …`),
  so a scan always explains why it went one way and not the other.
- **Meta-label learning layer (roadmap item 38) — the desk can now learn from
  its own outcomes, in shadow mode:** `signaldesk learn train` joins the
  ledger with resolved outcomes into a training set (demo/open rows excluded),
  fits a small L2 logistic model (numpy only, no new dependency) that predicts
  P(r_net > 0) from the decision-time features the ledger already freezes
  (score, entry mode, cost-in-R, chase context), evaluates it walk-forward
  only (expanding window, per-window imputation and standardization — nothing
  leaks), and saves a fingerprinted `learn_model.json`. `signaldesk learn
  report` shows the honest out-of-sample skill (AUC, log-loss vs base rate,
  expectancy by predicted-probability tercile) plus a clearly flagged
  in-sample history view. Every scan then stamps `ml_score`/`ml_fingerprint`
  on its new ledger records — and gates nothing: the score may only filter or
  size signals after a pre-registered trial closes positive on this evidence
  (rules R7/R6). Labels are read exactly as resolved by the frozen triple-barrier
  conventions; a missing or unreadable model degrades to "no score" (R4).
- **Regime gates (roadmap item 32, pre-registered as trial T2) — longs are
  refused when the trade would be a chase:** signals land in the avoid list,
  with cited trigger values, when any gate fires: price below its 200d SMA;
  annualized volatility above 150% (mania/panic extremes); 7d return above
  +50% (parabolic); price more than 25% over SMA20 (extension); and BTC
  below its own 200d SMA caps the whole crypto book. Symbols with under 200d
  of history are disclosed as unevaluated rather than assumed to pass.
  Thresholds are declared policy (`strategy/scoring.py` REGIME_* constants),
  cited in every report.
- **News depth — the scan now reads the news instead of just finding it:**
  context and catalyst lines quote the linked article's own opening text
  (Tavily extract when a Tavily key is configured, otherwise a direct page
  fetch with a stdlib HTML stripper and a 45 s per-scan budget). The search
  snippet remains the fallback when a page can't be fetched, and demo scans
  stay offline. Article text is cited with its URL
  (`column="extracted_text"`).
- **Altcoin Season Index rebuilt (blockchaincenter's API is gone, 404):**
  now computed CoinGecko-native from the index's original definition — the
  share of the top-50 coins outperforming BTC over the last 30 days — using
  the same one-call endpoint the movers tool already uses. Same CSV
  contract (`index/value/regime`); degrades with disclosure when the fetch
  fails.
- **Recommended position sizing (roadmap item 30):** every signal in a scan
  report now carries a sizing recommendation — inverse-volatility account
  risk in the 0.5–1% band (median-volatility signal = 0.75%), a cluster cap
  scaling the whole market's book down at 3% total account risk (correlated
  crypto signals are ~1.4 independent bets, not ten), and the implied
  notional so leverage is visible. Advice only: SignalDesk never places
  orders, and the outcome accounting keeps measuring the plan's own R unit
  (rule R7). Shown in the report's "Risk units & costs" table.
- **Signal-quality package (pre-registered as trial T1):** the first real
  outcome sample (76 resolved, −0.35R expectancy, 0% TP1 hits, 20/20 losing
  market-order entries, 91 same-day duplicate bets) drove three changes,
  declared via the new trial log before they start producing signals:
  - Emission dedupe (trial T1): the ledger keeps only the **first signal per
    (market, symbol, decision day)** — the same idea re-emitted later the
    same day (refined entry plan, re-run scan) is one bet, not a new one.
    Backfill imports history as-recorded.
  - Signal-time context frozen into every ledger record: 24h change (chase
    intensity), volume vs 30d average, RSI(14), SMA20/SMA50 distance %, and
    BTC 20d momentum (crypto) — the conditioning features the first sample
    lacked, enabling regime/chase analyses after the fact.
  - Trial log (roadmap item 33 foundation): `signaldesk trial add|list|close`
    over append-only `trials.jsonl` — declare hypothesis, change, judging
    criteria and minimum sample BEFORE a change runs; criteria are never
    edited afterwards (rule R7).
- **Live mark-to-market for open signals (WF-5):** open signals now show
  where they stand right now instead of a blank `+0.00R`: the current price
  and the P&L as a % of entry (plus unrealized R), colored green/red. The
  server loop refreshes the snapshot (`mtm.json`) on every hourly check; the
  dashboard also fetches on open and every 5 minutes via the throttled
  `POST /api/mtm/refresh` (and a "Refresh prices" button). Direction-aware
  using the signal's own risk unit. A snapshot, not a resolution: barrier
  hits still confirm only on closed daily bars (rule R7 unchanged), and a
  fully degraded quote fetch keeps the previous snapshot (rule R4).
- **Universe integrity (roadmap item 29) — keep the outcome sample honest:**
  - Liquidity screen in WF Phase 2: movers picks below declared crypto floors
    ($5M 24h volume / $50M market cap, cited in the report like the cost
    model) never reach scoring and are disclosed; names without tape data are
    kept (a missing tape is not evidence of illiquidity); FX/metals — no
    centralized tape — are not screened. Demo scans skip the screen (synthetic
    data).
  - Point-in-time survivorship audit (`signaldesk universe`): classifies every
    past run's universe (`runs/*/report.json`) against today's bars — active /
    dormant (no fresh bar in N days) / no data — and reports the share of
    ledger signal symbols that no longer trade. Saved to
    `universe_audit.json`; `signaldesk outcomes` and the Outcomes dashboard
    attach the caveat to the statistics once an audit exists.
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
- **Demo market rebuilt: real prices + a bear regime that exercises SHORT
  signals.** The demo generator's base prices were stale (ETH showed ~11k vs
  ~2.7k real); every crypto demo series is now calibrated so it ends exactly
  at the real 2026-09-30 price (majors from live quotes, alts from yfinance;
  SUI/TAO estimated where DNS blocked the fetch). The demo regime flipped
  from an all-bull uptrend to a mild bear — BTC drifts below its 200d SMA, so
  the declared item-32 mirror gate opens the SHORT book and demo scans emit
  SHORT signals (TAO/BNB/SUI-class setups) with the long book standing down,
  disclosed per symbol in the avoid list. Demo Fear & Greed moved to 38
  (fear regime) to match. This makes the demo demonstrate both engine
  directions across regimes instead of only ever showing LONGs. Tests that
  assumed the all-bull demo (entry-plan attachment, markdown sections) are
  direction-aware now.
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
- **The 200d regime gate works on live scans now:** the daily OHLCV fetch
  window was 6mo (~126 bars), so SMA200 could never compute outside demo data
  — the item-32 trend gates were permanently unevaluated and every brief
  showed "SMA200 n/a". The window is 1y (TAD: gates need 200 bars).
- **The AI reads articles, not page chrome:** Tavily's raw content came back
  with markdown image/link wrappers and menu boilerplate; `extract.py` now
  strips images, unwraps links and drops leading navigation runs before the
  text reaches the model.
- **Off-topic search hits no longer become "market news":** a hit whose
  title/snippet bears no relation to the query (a bank menu page ranked for
  "bitcoin price drivers") is skipped with a disclosure instead of being fed
  to the model as a market-context claim.
- **STX-USD downloads again:** Yahoo renamed the Stacks ticker to
  `STX4847-USD` (numeric suffix for the collision with Seagate's STX equity),
  so every scan carrying STX from the movers list failed its download.
  `markets.vendor_symbol` now maps collision-renamed tickers
  (`YF_CRYPTO_ALIASES`) for quotes, OHLCV and mark-to-market alike.
- **No more LiteLLM footer spam:** LLM failures used to print litellm's
  "Give Feedback / Get Help" block on every call; the call sites now set
  `litellm.suppress_debug_info = True` and the real error already surfaces in
  the run trace's disclosures (R4).
- The `r_net` column in the Outcomes signals table is now sign-colored like
  the live column: green (`#3fb950`) for `>= 0`, red (`#f85149`) for negative
  — resolved values rendered as plain text before.
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
