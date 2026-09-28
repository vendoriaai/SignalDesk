# PRD — Open-Source AI Market Research & Signal Agent

**Working title:** SignalDesk (placeholder — rename freely)
**Version:** 1.0 · **Status:** Draft · **Date:** 2026-09-21
**Author / Owner:** Project founder

---

## 1. Vision

A free, open-source, cross-platform desktop application (Windows, macOS, Linux) that gives any user a Perplexity-Computer-Work–style AI research agent for financial markets. The user asks in natural language — e.g., "scan the whole crypto market and find the best pair to trade now" — and the agent autonomously performs multi-source research (market data, news, technical charts, sentiment indices, catalysts), computes indicators in a sandboxed Python environment, and returns cited, ranked trading signals with entries, stop losses, take profits, and confidence scores.

## 2. Assumptions (pending founder confirmation)

| # | Assumption | Default choice | Needs confirmation |
|---|-----------|----------------|--------------------|
| A1 | LLM access | BYOK (user provides OpenAI/Anthropic API key) + optional local model via Ollama | ☐ |
| A2 | Database/auth | Hosted Supabase (free tier): Auth (email) + Postgres | ☐ |
| A3 | App form | Local Python backend (FastAPI) + browser-view desktop wrapper, packaged per-OS | ☐ |
| A4 | Monetization | None in v1; prompt collection is for product improvement + user history | ☐ |
| A5 | Data sources for launch | yfinance, CCXT/Binance public API, Alpha Vantage (free tier), CoinGecko, FRED, free news endpoints/rss, Tavily/exa or DuckDuckGo for web search | ☐ |
| A6 | Scope | Analysis and signals only — strictly read-only. No broker integration, no auto-trading | ☐ |
| A7 | Prompt/result collection | Opt-in checkbox at signup; used for analytics and perceived as user history | ☐ |
| A8 | Target user | Retail crypto/forex/metals traders, solo-maintainable project | ☐ |

## 3. Goals and Non-Goals

**Goals**
- One-click install on Windows (.exe/NSIS), macOS (.dmg), Linux (AppImage/.deb).
- "Computer use" parity for finance: the agent plans, calls tools, writes and runs Python, cites every number, and produces a final report — without manual prompting between steps.
- Reference workflow (canonical "Market Scan"): news context → universe discovery → quotes → OHLCV history → indicator computation → cross-asset comparison → sentiment indices → per-asset catalyst research → ranked signal report. See `workflows.md`.
- User accounts with cloud-synced history, watchlists, and prompt/result logging (Supabase).
- Fully read-only tool layer at launch.

**Non-Goals (v1)**
- Order placement, broker APIs, portfolio management.
- Real-time tick-level streaming (optional stretch).
- Mobile apps.
- Paid tiers / Stripe.

**Stretch for v1.1+**
- Scheduled background scans with alerts.
- Backtesting engine validation of emitted signals.
- MCP server support so users plug in third-party data tools.

## 4. User Stories

1. As a trader, I type "scan the crypto market and find the best pair to trade now" and receive a ranked list of signals with citations within ~3 minutes.
2. As a user, I can sign up by email, log in on any of my machines, and see my full conversation/report history synced.
3. As a user, I can browse the agent's execution trace (each search, each data pull, each computed indicator) to verify how conclusions were reached.
4. As a user, every numeric claim in the report (price, RSI, MACD, ATR, returns) is citable — I can trace it to the underlying CSV row and formula.
5. As a user, I can maintain custom watchlists ("my forex pairs", "my altcoins") that the agent uses as its scan universe.
6. As the product owner, I can see anonymized prompt/outcome analytics to improve agents and prompts.

## 5. Functional Requirements

### FR-1 Authentication & Accounts
- FR-1.1 Email/password signup and login via Supabase Auth; session token stored in OS keychain.
- FR-1.2 Password reset flow. FR-1.3 Single sign-off (sign out revokes local session).
- FR-1.4 Prompt/result data collection requires an opt-in consent checkbox shown at signup; users may toggle it later in Settings; when off, history is stored locally only.

### FR-2 Agent Engine
- FR-2.1 Natural-language task understanding and autonomous multi-step planning (planner worker critic loop).
- FR-2.2 Read-only toolset (see FR-3). No write-side exchange/broker tools ship in v1.
- FR-2.3 Sandboxed Python executor (local subprocess, restricted stdlib whitelist: pandas, numpy, matplotlib for charts; no network from inside sandbox; input via temp files only).
- FR-2.4 Streaming progress UI: each step (search query, tool call, code block, computed table) appears live, mirroring the sample workflow trace.
- FR-2.5 Citation system: every figure carries provenance (source, file/dataset, row key, column, and for derived values: formula + derived-from).
- FR-2.6 Report synthesis: final answer = market summary + ranked signals table (pair, direction, entry, SL, TP, R:R, confluence notes, citations) + a risk-unit/cost table (R, cost-in-R, break-even win rate) + risk disclaimer.
- FR-2.7 Outcome measurement: every emitted signal is recorded with its levels, assumed cost and rule fingerprint, and can be resolved later (TP1/TP2/stop/time barrier) into expectancy-in-R statistics — with intervals and sample-size caveats (rule R7).

### FR-3 Data Tools (v1 toolset)
- Quotes (stocks, ETFs, crypto, indices): yfinance + Binance/Bybit public endpoints.
- OHLCV history (1m–1d): yfinance; CCXT for crypto; Alpha Vantage for forex/XAUUSD.
- Market gainers / losers / most active (crypto & US equities).
- Fundamentals US equities: financials, ratios, estimates, analyst research, earnings (yfinance + SEC EDGAR; Financial Modeling Prep-compatible adapter).
- Macro snapshot/history: FRED API.
- ETF holdings, institutional holders, insider transactions, congressional trades (v1.1 via SEC/Capitol Trades scrapers or MCP server).
- Market sentiment: Fear & Greed Index, Altcoin Season Index (alternative.me / CoinMarketCap-scraper / blockchaincenter endpoints).
- Web search & news research: pluggable search provider (Tavily/exa API key, or DuckDuckGo fallback), plus per-asset catalyst news queries.

### FR-4 Storage & Sync (Supabase)
- Store per user: sessions, prompts, agent run traces, final reports, cited artifacts metadata, watchlists, emitted signals (for later outcome scoring).
- RLS: every table row scoped to owner (user_id) unless explicitly shared.
- Local-first cache (SQLite) so history is browsable offline; sync-on-connect via upsert.

### FR-5 UI
- Chat-style agent view with collapsible step trace; Markdown table rendering; chart preview; copy/export report (Markdown, PDF).
- History sidebar, watchlist manager, Settings (LLM provider/keys, search provider/keys, data-provider keys, consent toggle, theme).

## 6. Non-Functional Requirements

- Cross-platform: single codebase builds Win/mac/Linux installers via CI (GitHub Actions + PyInstaller; unsigned binaries documented, code-signing tracked as infra task).
- Performance: full crypto scan (top ~100 by volume) + news + TA < 4 minutes on consumer hardware with cloud LLM.
- Reliability: any tool failure degrades gracefully — report notes missing inputs rather than failing outright.
- Security: API keys stored in OS keychain only; sandbox blocks network and filesystem escapes; all exchange calls read-only.
- Licensing: MIT or Apache-2.0; third-party API ToS complied with (rate limits honored, no scraping against ToS).

## 7. Success Metrics

- % of "scan market" tasks that complete end-to-end with a synthesized report (target ≥ 95%).
- Mean scan time; citation coverage (% of numeric claims with provenance; target 100%).
- D7 retention of signed-up users; weekly scans per active user.
- Signal outcome tracking (shipped at CLI level; dashboard pending): expectancy in R net of cost with confidence intervals, hit rate, profit factor and time-to-TP1 from the ledger + triple-barrier resolver — reported with explicit sample-size caveats, never as a forecast.

## 8. Compliance & Safety

- Every report begins/ends with a "not financial advice" risk disclosure; signal confidence is presented as backtest-informed probability, never a guarantee.
- Prompts/results sent to Supabase only with consent; PII limited to email; export/delete-my-data supported (GDPR-style erasure).
- No promised returns language anywhere in UI/docs.

## 9. Milestones (summary — detail in roadmap.md)

- M1 Core agent loop + crypto scan workflow (headless CLI).
- M2 Toolset breadth (forex/XAUUSD, equities, macro, sentiment) + citation layer.
- M3 Desktop UI + auth + Supabase sync.
- M4 Packaging/installers + public release.
- M5 Scheduled scans, backtest validation, MCP plugins.
