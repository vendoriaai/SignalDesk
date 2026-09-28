# TAD — Technical Architecture Document

**Project:** SignalDesk · **Version:** 1.0 · **Date:** 2026-09-21
**Companion docs:** `prd.md`, `workflows.md`, `data-model.sql`, `roadmap.md`

---

## 1. System Overview

SignalDesk is a Python desktop application composed of five subsystems:

```
┌────────────────────────────────────────────────────────────┐
│                      Desktop Shell                          │
│   pywebview window ← serves local FastAPI + React/Vue UI    │
├────────────────────────────────────────────────────────────┤
│                     FastAPI Backend (local)                 │
│   /chat  /runs  /history  /watchlists  /settings  /auth     │
├───────────────┬───────────────┬───────────────┬────────────┤
│  Agent Engine  │  Tool Layer   │  Compute       │  Sync      │
│  (LangGraph)   │  (read-only)  │  Sandbox       │  (Supabase)│
│  planner →     │  market data  │  subprocess    │  auth,     │
│  researcher →  │  web search   │  pandas TA     │  history,  │
│  analyst →     │  sentiment    │  charts        │  telemetry │
│  critic →      │  fundamentals │                │            │
│  synthesizer   │  macro        │                │            │
└───────────────┴───────────────┴───────────────┴────────────┘
```

### Why this shape
- **Local-first:** all inference orchestration and computation run on the user's PC; Supabase is only accounts + history/analytics.
- **Web UI in a desktop wrapper** (pywebview over a localhost FastAPI) gives one UI codebase across OSes and avoids GUI-toolkit packaging pain. Alternative (PySide6) rejected to keep the UI iterable; can be revisited.
- **LangGraph** state machine for the agent: deterministic, resumable, observable — each node/step streams to the UI and is persisted as a trace row.

## 2. Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | single language end-to-end |
| Agent orchestration | LangGraph + LiteLLM | provider-agnostic LLM calls (OpenAI/Anthropic/Gemini + Ollama local) |
| Backend API | FastAPI + Uvicorn | serves UI, WebSocket progress stream |
| Desktop wrapper | pywebview | uses native WebView (Edge WebView2 / WKWebView / WebKitGTK) |
| Market data | yfinance, ccxt, alpha_vantage, FRED API, CoinGecko, alternative.me | all free tiers, adapters behind a uniform `Tool` interface |
| Web search | Tavily or exa (BYOK); DuckDuckSearch fallback | mirrors the sample workflow's `search.web_by_query` batch calls |
| Compute | pandas, numpy, TA (pandas-ta or hand-rolled RSI/MACD/ATR as in sample), matplotlib | runs in restricted subprocess |
| Local caching | SQLite + SQLModel | offline history, upsert-based sync queue |
| Auth | Supabase Auth (gotrue) via supabase-py; tokens in OS keychain (keyring) |
| Cloud DB | Supabase Postgres with RLS (schema: `data-model.sql`) |
| Packaging | PyInstaller → NSIS (.exe)/hdiutil (.dmg)/AppImage on GitHub Actions |
| Tests | pytest, agent eval harness with recorded fixtures (VCR for HTTP) |

## 3. Module Breakdown

### 3.1 `agent/` — Orchestration engine
LangGraph `StateGraph` with nodes:

1. **planner** — parses the user task, selects a workflow template (`market_scan`, `deep_dive_ticker`, `forex_scan`, `earnings_research`, `policies_macro`), produces a step plan.
2. **researcher (news/web)** — batched web-search queries via the search provider; reads top pages; extracts dated claims ([search results in the sample workflow: market context, per-asset catalysts, Fed/macro queries]).
3. **data_runner** — executes read-only market tools; every CSV result is persisted to the artifact store.
4. **analyst (compute)** — writes/generates Python analysis scripts (RSI, MACD, SMA/EMA stacks, ATR, volatility, multi-window returns) and runs them in the sandbox.
5. **citicizer** — attaches citation objects (source, dataset file, row key, column; derived values carry `formula` + `derived_from`) exactly as in the sample trace.
6. **critic** — validates: all requested assets analyzed? numbers cited? contradictions flagged? Loops back to planner if insufficient (max 2 loops).
7. **synthesizer** — emits the final report (Pydantic `ScanReport` schema → Markdown + JSON sidecar).

State is a typed dataclass streamed over WebSocket: `{plan, steps[], artifacts[], citations[], partial_report}`.

### 3.2 `tools/` — Uniform read-only tool interface
```python
class Tool(BaseModel):
    name: str; description: str
    async def run(self, **kwargs) -> ToolResult: ...
class ToolResult(BaseModel):
    csv_files: list[Path]; summary: str; sources: list[Source]
```
Adapters: `YFinanceQuotesTool`, `CCHistOHLCVTool`, `AlphaVantageFxTool`, `FredMacroTool`, `GainersLosersTool`, `FearGreedTool`, `AltSeasonTool`, `FundamentalsTool` (yfinance+EDGAR), `WebSearchTool`. Rate-limiting per adapter via token-bucket. All tools are registered centrally; MCP support in v1.1 lets users add external MCP servers (e.g., community finance MCPs).

### 3.3 `sandbox/` — Compute executor
- `ProcessPool` subprocess with: no network (OS-level stub/import hook), temp-dir-only file access, per-call timeout (default 60 s; the chart-heavy scan passes pass 180 s — rendering the full universe legitimately exceeds 60 s on slow machines), memory cap.
- Input/output contract: reads artifacts from `artifacts/`, writes `output/` (CSV/PNG); stdout captured into the step trace.
- Library whitelist enforced at import time (`pandas`, `numpy`, `math`, `matplotlib`, `csv`, `json`, `sqlite3`).

### 3.4 `citations/` — Provenance system
Direct citation: `{value, label, source_tool, file, row_key, column, metadata_ref}`.
Derived citation: `{value, label, formula, derived_from: [citation_ids]}` — mirroring the sample's `cite()` calls for close prices (direct) vs RSI/MACD/ATR/returns (derived).
Citations persist per run and render inline in the report UI.

### 3.5 `sync/` — Supabase layer
- Auth: email+password; refresh tokens via keyring; `GET /auth/me` gate at app start.
- Repositories: `SessionsRepo`, `PromptsRepo`, `RunsRepo`, `ArtifactsRepo` (metadata only; heavy CSVs stay local, optionally uploaded to Supabase Storage if consent), `WatchlistsRepo`, `SignalsRepo`.
- Sync engine: local SQLite queue → batched upserts; conflict rule "last writer wins" (watchlists only mutable store).
- Telemetry: prompt text + report outcome stored only when `consent_prompts = true` (RLS + server-side check).

### 3.6 `ui/` — Frontend
Vite + React (or Vue): Chat view with streaming step cards (Search / Tool call / Code / Result / Citation), report renderer with tables and charts, History, Watchlists, Settings (LLM provider + key, search provider + key, data keys, consent, appearance).

### 3.7 `packaging/`
GitHub Actions matrix (windows-latest, macos-14, ubuntu-latest): pip → PyInstaller spec → installer build → artifact publish to GitHub Releases. Auto-update via a tiny update-manifest check (TUF-style; v1 can ship manual download).

### 3.8 Measurement layer — `costs.py` · `ledger.py` · `outcomes.py` · `metrics.py` · `resolver.py` · `scheduler.py` · `paper.py` · `mtm.py` · `universe.py`
Deliberately framework-free (no backtesting engine): signals are discrete
records with levels, so the honest evaluation is a resolver over OHLCV
artifacts, not a position-accounting engine.

- `costs.py` — per-market/per-symbol round-trip cost assumptions (% of
  notional: fees + spread + slippage) plus `cost_in_r` and
  `breakeven_win_rate(c, m) = (1+c)/(m+1)`. Cited in reports as
  `source_tool="cost_model"`; never presented as market data.
- `ledger.py` — append-only `signals.jsonl`: actionable levels + entry mode,
  risk and risk%, cost-in-R, sha256 of the OHLCV snapshot, decision bar date,
  scoring-weights fingerprint, demo flag, app version. `backfill()` imports
  `runs/*/report.json` so historical signals are scoreable immediately.
- `outcomes.py` — triple-barrier resolver (TP1/TP2/stop/time barrier; next-bar
  start; pessimistic same-bar tie-break; MFE/MAE; three pre-computed R variants
  so alternative exit policies can be scored without re-resolving).
  `outcomes.jsonl` is append-only and read as "latest per signal_id".
- `metrics.py` — expectancy in R net of cost with a week-block bootstrap CI
  (signals in one week are one correlated bet), Wilson hit-rate interval,
  profit factor, median bars-to-TP1, breakdowns, and explicit small-sample
  caveats. Surfaced by `signaldesk outcomes` / `signaldesk ledger`.
- `resolver.py` — the shared resolution pipeline (WF-5, item 28): ledger
  selection + filters → per-market daily bars → triple-barrier →
  `outcomes.jsonl` → joined rows. The only entry point for resolution; the
  CLI `outcomes` command, `POST /api/outcomes/resolve` and the scheduler all
  call it. `signal_rows()` joins ledger + latest resolutions for the
  dashboard without fetching.
- `scheduler.py` — server background loop (FastAPI lifespan): one resolution
  pass per local day (on startup when pending), hourly retry after failure;
  state in `outcomes_state.json` powers the dashboard's "last pass" line.
  Off-switch: `SIGNALDESK_SCHEDULER=0`.
- `paper.py` — append-only `paper.jsonl` of fill/miss decisions per signal
  (first decision wins; events require a known ledger signal_id). Stats:
  fill rate, direction-aware entry gap in R vs the modelled entry, crypto
  funding drag over resolved holds. Additive to the R7 accounting — outcomes
  keep resolving the modelled plan; paper events measure the
  execution-vs-model gap without re-labelling anything.
- `mtm.py` — live mark-to-market for open signals: the scheduler's hourly
  check also refreshes a quote snapshot (`mtm.json`) and computes
  direction-aware unrealized R per open signal (its own risk unit). Served
  with `/api/outcomes` as a cached read — request handlers never fetch
  quotes. A snapshot, not a resolution: barriers still confirm only on
  closed daily bars; a fully degraded fetch keeps the previous snapshot (R4).
- `universe.py` — universe integrity (item 29): the liquidity screen applied
  to WF Phase 2 movers picks (declared per-market floors, cited as
  `source_tool="liquidity_screen"`; a missing tape keeps the name) and the
  point-in-time survivorship audit (`signaldesk universe` →
  `universe_audit.json`): every historical run's universe classified against
  today's bars (active / dormant / no-data) plus the share of ledger signal
  symbols no longer trading. The caveat attaches to outcome statistics (CLI +
  dashboard). Read-only: never re-derives signals (same contract as the
  resolver).

API/UI surface: `GET /api/outcomes` (cached, never fetches bars),
`POST /api/outcomes/resolve`, `POST /api/paper/{signal_id}/fill|miss`, and the
Outcomes dashboard view in the React UI.

Contract: the ledger is the only source of truth about what was emitted; the
resolver never re-derives signals from history (avoids the selection bias of
re-scoring a survivor universe).

## 4. Data Flow — Canonical Market Scan

User prompt → **planner** builds plan → **researcher** runs batched news queries (market context, BTC drivers, weekly outlook) → **data_runner** fetches gainers/losers + universe selection → quotes CSV → OHLCV CSVs per candidate → **analyst** computes RSI/MACD/SMA/EMA/ATR/volatility/3-7-30d returns in sandbox → **researcher** second pass (sentiment indices: Fear & Greed, Alt Season; per-asset catalyst queries; ETF flows; Fed/FOMC) → **citicizer** attaches provenance to every number → **critic** verifies coverage → **synthesizer** emits ranked signal table with entry/SL/TP (ATR-based) and citations → persist run + report (+ signals) → Supabase sync. Full step-by-step spec: `workflows.md`.

## 5. Security & Privacy

- All exchange/data calls read-only. Tool registry rejects any `send_/create_/update_/delete_`-named plugin unless a v1.1+ "trading" capability flag is on (default off).
- Sandboxed evaluation as above; LLM keys never leave the keychain; HTTPS-only outbound.
- RLS enforced for all user tables (see `data-model.sql`); service-key usage confined to a minimal optional server shim (none needed v1 — client uses anon key + user JWT).
- Erasure: Settings → "Delete my cloud data" issues cascaded delete for the user_id.

## 6. Observability & QA

- Every run produces a replayable trace (JSON) — the UI "execution trace" and evaluation harness both consume it.
- Eval levels: (1) unit tests, (2) workflow tests on the demo market (offline, deterministic), (3) the 30-case planner/workflow corpus (`evals/cases.json`) with schema/tool-call assertions, and (4) the **outcome layer** — the ledger + triple-barrier resolver + `metrics.py`, which is the only level that measures P&L-like results rather than shape. Level 4 is the input to any strategy change: a rule tweak is judged by expectancy in R net of cost on a paired signal set, with intervals, never by the in-sample score.
- LLM eval: critic pass-rate, citation coverage measured per run and (optionally) reported to owner analytics.

## 7. Open Design Decisions

- D1: LLM default provider order — propose: user-selected at onboarding, default Anthropic/OpenAI BYOK, Ollama optional.
- D2: Search provider default — Tavily (free 1k calls/mo) vs DuckDuckGo (unlimited, weaker). Recommend Tavily BYOK-inspired default with DDG fallback.
- D3: Signal scoring formula governance — editable `strategy/scoring.py` module with versioned presets so the community can fork strategies.
- D4: PDF export via reportlab (already constraint-compatible).

## 8. Repository Layout

```
signaldesk/
  __init__.py  config.py  cli.py  api.py  desktop.py  updates.py
  agent/                # planner + events
  tools/                # read-only adapters (yfinance, coingecko, alphavantage,
                        #   macro, sentiment, equities, search, demo, demo_equities)
  sandbox/executor.py   # restricted subprocess
  citations/            # direct + derived provenance
  analysis/             # indicators.py, charts.py
  strategy/scoring.py   # presets, trade plans, risk floor, entry refinement
  workflows/            # market_scan.py (WF-1/3/4), deep_dive.py (WF-2), entry_refine.py
  report/               # schema.py, render.py
  costs.py  ledger.py  outcomes.py  metrics.py   # measurement layer (TAD 3.8)
  resolver.py scheduler.py paper.py              # WF-5: pipeline, daily loop, paper log
  universe.py                                    # liquidity screen + survivorship audit (item 29)
  markets.py  watchlists.py  store.py  userconfig.py  sync.py
  ui/                   # React app (dist/ served by api.py)
  evals/                # planner/workflow corpus runner
tests/                  # pytest suite (+ new modules must add coverage here)
packaging/              # pyinstaller spec + NSIS script
.github/workflows/      # installer builds on v* tags
docs/ prd.md tad.md workflows.md roadmap.md data-model.sql update-manifest.json
```
