# Roadmap — SignalDesk Build Plan (step by step)

**v1.0 · 2026-09-21** · Assumes 1 founder-developer, evenings/weekends pace ~20 h/week. Adjust dates at will.

---

## Phase 0 — Foundations (Week 1)

1. Create repo, `uv`/`poetry` project, Python 3.12, ruff, pre-commit, MIT license, CI skeleton (GitHub Actions).
2. Run `data-model.sql` on hosted Supabase project; create buckets none yet (artifacts local-only v1).
3. Config system: `.env` + OS keychain for `LLM_API_KEY`, `SEARCH_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`.
4. **Exit test:** `supabase-py` signup + login + insert/select a test session with RLS verified.

## Phase 1 — Headless Core Agent (Weeks 2–4)

5. Implement `tools/` uniform interface + adapters: yfinance quotes/OHLCV, CCXT Binance OHLCV, web search (Tavily, DDG fallback).
6. Build sandbox executor (restricted subprocess, whitelist imports, 60 s cap).
7. Implement citation engine (direct/derived) + artifact store.
8. Port the reference workflow into `workflows/market_scan.py` (WF-1 all 8 phases), planner hard-coded to WF-1 first.
9. Add `strategy/scoring.py` preset `trend-momentum-v1` exactly per workflows.md Phase 7.
10. Wire LangGraph graph: planner → researcher → data_runner → analyst (sandbox) → researcher₂ → criticizer → critic → synthesizer; stream events to stdout/JSONL.
11. **Exit test:** CLI command `signaldesk scan crypto` reproduces the reference run end-to-end on free data: news batch → gainers/losers → quotes → 90d OHLCV for 12+ symbols → RSI/MACD/ATR/vol/returns → Fear&Greed + Alt-Season + catalysts → cited ranked report ≤ 4 min. Golden eval asserts: ≥ 20 citations, report parses into `ScanReport` schema, no uncited numbers.

## Phase 2 — Toolset & Workflow Breadth (Weeks 5–7)

12. Forex/metals adapters (Alpha Vantage FX, yfinance XAUUSD=X), WF-3.
13. US equities tools: fundamentals, ratios, estimates, earnings calendar/history (yfinance + EDGAR) → WF-2, WF-4 (watchlist scan).
14. Macro snapshot via FRED; integrate into forex scoring weights.
15. LLM planner unlocked: prompt → workflow template selection (WF-1..4) with parameter extraction.
16. **Exit test:** eval harness passes 30 canned tasks incl. "scan forex", "XAUUSD vs BTC this month", "deep dive AAPL before earnings".

## Phase 3 — Desktop UI, Auth & Sync (Weeks 8–11)

17. FastAPI backend endpoints + WebSocket run streaming; SQLite local cache + SQLModel.
18. React UI: chat view with phase cards (plan/search/tool/code/result), citation popovers, report renderer, history sidebar, watchlist manager.
19. Settings: LLM provider/key, search provider/key, consent toggle, "delete my cloud data".
20. Supabase signup/login UI; keychain token storage; sync engine (upsert queue, last-write-wins for watchlists).
21. pywebview shell launching local server; OS-native window + tray quit.
22. **Exit test:** fresh laptop → install deps →	create account → scan → close app → reopen — history loads offline; second machine shows synced history.

## Phase 4 — Packaging & Public Release (Weeks 12–14)

23. PyInstaller specs; CI matrix builds: Windows .exe (NSIS), macOS .dmg (aarch64+x86 universal note), Linux AppImage + .deb.
24. Update-manifest endpoint + in-app "update available" notice (manual download accepted for v1; TUF auto-update in 1.1).
25. Docs site (mkdocs): quickstart, data-source guides, strategy authoring, privacy page.
26. Onboarding UX: first-run wizard = pick LLM (BYOK or local Ollama) → connect Supabase account → consent screen → run demo scan.
27. `v1.0` GitHub Release + announcement assets (demo GIF of the scan workflow).

## Phase 5 — Post-1.0 (Weeks 15+)

**P0 measurement foundation — shipped 2026-09-27** (see CHANGELOG): signal
ledger (`signals.jsonl` with snapshot hashes + weights fingerprint),
triple-barrier resolver, `signaldesk outcomes`/`ledger` CLI (expectancy in R net
of cost with block-bootstrap CI, Wilson hit rate, profit factor, time-to-TP1,
MFE/MAE, breakdowns), per-instrument cost model with cost-in-R, and the
cost-aware risk floor in Phase 7/7.5 (refinement may tighten R by at most ~25%,
never below max(0.75×ATR(1d), 15× round-trip cost)). Evidence and the ordered
plan below come from the 2026-09-26 strategy review; each item is judged by the
outcome layer, not by in-sample score.

28. ✅ **Shipped 2026-09-28** — WF-5 remainder: scheduled daily resolution
    (`scheduler.py`: one pass per local day while the server runs, hourly
    retry on failure) + UI stats dashboard (Outcomes view over
    `GET /api/outcomes`; `POST /api/outcomes/resolve` for on-demand passes);
    paper ledger with execution-vs-model gap logging (`paper.py`:
    fill/miss decisions, entry slippage in R, missed fills, crypto funding
    drag). One shared pipeline (`resolver.py`) serves CLI, API and scheduler.
    Extension (same day): live mark-to-market — the hourly server check also
    refreshes a quote snapshot (`mtm.py` → `mtm.json`) so open signals show
    unrealized R on the dashboard between resolution passes.
29. ✅ **Shipped 2026-09-28** — Universe integrity: liquidity screen on movers
    picks (declared floors — crypto $5M 24h volume / $50M market cap, cited in
    the report; a missing tape keeps the name; FX/metals untouched) + the
    `signaldesk universe` point-in-time audit (every run's
    `report.json` universe classified against today's bars: active / dormant /
    no-data; survivorship exposure of the ledger's signal symbols; caveat
    attached to outcome statistics and the dashboard).
30. Position sizing: inverse-volatility risk 0.5–1% per trade plus a
    per-asset-class cluster cap — crypto signals run 0.6–0.9 correlated, so ten
    simultaneous longs are ~1.4 independent bets, not ten.
31. Exits, tested not assumed: A/B the current 50/50 TP1/TP2 policy against a
    larger first tranche + structure trail, and test a time stop; the outcome
    layer already stores `r_all_in` / `r_runner` so policies can be re-scored
    without re-resolving.
32. ✅ **Shipped 2026-09-29** — Regime gate, pre-registered as trial T2:
    200-day trend filter (per symbol; BTC below its 200d SMA caps the whole
    crypto book) plus an extremes-only volatility filter (>150% annualized),
    extended with an anti-chase extension cap (7d gain > +50% or price > 25%
    over SMA20 — the audit's 0/20 chase failure mode). Continuous vol
    targeting stays out (failed out-of-sample).
33. Trial log + pre-registration shipped 2026-09-28 (`signaldesk trial
    add|list|close`, append-only `trials.jsonl`; first trial T1 = emission
    dedupe + context enrichment). Remaining: deflated Sharpe / PBO once >20
    trials are logged; purged CV only if parameters start being fitted.
34. Backtest validation module (vectorbt) feeding calibrated confidence into scoring.
35. MCP client support for community finance servers.
36. Scheduled background scans + OS notifications; watchlist alerts.
37. Optional hosted relay (if BYOK adoption lags) — keep strictly out of v1 scope.

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Free LLM-less local inference too weak for planning | High | Default BYOK; planner prompts tuned small-model-friendly as fallback |
| yfinance/Binance rate limits on 100-symbol scans | Medium | Symbol cap (20 default), token-bucket limiter, per-user cache |
| Search quality via DDG poor | Medium | Recommend Tavily at onboarding; keep DDG as documented fallback |
| PyInstaller Mac signing friction (unsigned app warning) | High | Document Gatekeeper workaround; budget Apple cert later |
| Regulatory exposure from "signals" wording | Medium | "Analysis/research assistant" framing; disclaimers; no auto-trading |
| Sentiment-index endpoints unstable (scraped) | Medium | Adapter isolation + degrade-and-disclose rule R4 |

## Definition of Done (v1)

A new user can install on any OS, create an account, paste "scan the crypto market and find the best pair to trade now", watch the agent execute the reference workflow live, receive a fully cited ranked signal report within 4 minutes, and find the report synced on a second device.
