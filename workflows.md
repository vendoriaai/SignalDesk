# Workflows — Agent Execution Specs

**Project:** SignalDesk · v1.0 · 2026-09-21
This document defines the agent's executable workflows step-by-step, derived from the founder's reference trace (a full Perplexity-Computer-Work crypto scan). Each workflow is a LangGraph template the planner instantiates. Every step lists its tools, inputs/outputs, and failure handling.

---

## WF-1 · Market Scan & Signal Generation  *(canonical — from reference trace)*

**Trigger phrases:** "scan the crypto market", "scan forex", "scan XAUUSD", "which pair is best to trade now".
**Output:** `ScanReport` — market summary + ranked signals table with full citations.

### Phase 0 — Plan & Tool Discovery
1. Planner parses market ("crypto" | "forex" | "metals/XAUUSD" | "stocks"), timeframe (default daily, last 90 days), and universe size (default: top movers + majors).
2. Resolver lists available tools/adapters and confirms coverage for the requested market (missing coverage → degrade, notify in report).

### Phase 1 — Market Context Research (web)
3. Batched web search (all queries in one call):
   - "{market} market news today {date}"
   - "{dominant asset} price surge today / why is {asset} up today"
   - "{market} market outlook this week"
4. Read top pages; extract dated claims into `context_claims[]` (each with source URL/title — these become web citations).
*Failure:* search provider down → fallback provider; all fail → report notes "news context unavailable".

### Phase 2 — Universe Construction
5. Call `market_gainers` and `market_losers` tools for the market; store CSVs as artifacts.
6. Merge movers with the market's majors list (crypto: BTC, ETH, BNB, SOL, XRP + top gainers like SUI/TAO/AAVE/ENA from the reference run; forex: majors + crosses; metals: XAUUSD/XAGUSD) → `universe[]` (cap 20; user-adjustable). Crypto movers picks pass a **liquidity screen** (item 29): rows below the declared floors — $5M 24h volume / $50M market cap, cited as `source_tool="liquidity_screen"` — are dropped and disclosed; a missing tape keeps the name (R4); FX/metals have no centralized tape and are not screened; demo scans skip the screen.

### Phase 3 — Market Data Pull
7. `quotes`(universe) → snapshot CSV (price, 24h change %, day/year low/high, volume). Cite each price cell.
8. Per symbol: `ohlcv_history`(symbol, range=~90d, interval=1d) → per-symbol CSV artifacts.  *(Reference: BTCUSD/SUIUSD/TAOUSD/XRPUSD/SOLUSD/AAVEUSD/ENAUSD/ETHUSD/BNBUSD daily CSVs 2026-07-01→2026-09-21.)*
*Failure:* symbol fetch error → drop from universe, log in trace.

### Phase 4 — Technical Computation (sandbox)
9. Sandbox script computes, per symbol (exactly as in the reference trace):
   - RSI(14) (Wilder-style rolling)
   - SMA20, SMA50; EMA9, EMA21 (+ EMA50/200 for higher timeframe variant)
   - MACD(12,26,9): line, signal, histogram; histogram delta vs prior bar (bullish cross detection)
   - ATR(14) (true range max of H−L, |H−Cprev|, |L−Cprev|)
   - Annualized volatility = stdev(20d returns) × √365 × 100
   - Returns: 3d, 7d, 30d %
   - 20-day swing high/low
   - Booleans: above_sma20, above_sma50, ema9_above_ema21, macd_above_signal, macd_bullish_cross
10. All computed values become **derived citations** (`formula=` string + `derived_from=[close-price citations]`), e.g.:
    `SUI RSI14 = 68.4 [formula RSI(close,14) ← SUIUSD close 2026-09-21]`.

### Phase 5 — Sentiment & Regime Research (web)
11. Batched queries:
    - "crypto fear and greed index today"
    - "altcoin season index today"
    - "bitcoin ETF ecosystem outlook {month year}" (or FX: "DXY outlook", metals: "gold outlook Fed")
    - "{market} Federal Reserve {month year} FOMC decision"
12. Extract index values (e.g., Fear & Greed 46 Neutral; Alt Season 47–76 range across providers — record per-source values, flag disagreement) → `sentiment[]` citations.

### Phase 6 — Per-Candidate Catalyst Research (web)
13. For the top ~5 technical candidates, batched queries: "{name} price surge news catalyst", "{name} news {month year}", plus sector flows ("spot bitcoin ETF flows this week").
14. Extract catalyst claims per asset → `catalysts[symbol][]` citations.  *(Reference: SUI institutional staking/CME futures; TAO AI-narrative; AAVE RWA/news; ETF inflow headlines.)*

### Phase 7 — Scoring & Signal Construction
15. Score each symbol via `strategy/scoring.py` (versioned preset `trend-momentum-v1`):

| Component | Weight | Condition (bullish) |
|---|---|---|
| Trend stack | 40% | close>SMA20, close>SMA50, EMA9>EMA21 (13.3% each) |
| Momentum | 25% | MACD hist > 0 (15%); hist increasing vs prior bar (10%) |
| RSI regime | 15% | 50≤RSI<70 full (15%); RSI 45–50 or 70–75 half; else 0 |
| Volume/lqi | 10% | volume ≥ 30d avg (10%) |
| Sentiment adj | 10% | market Fear&Greed neutral→greedy adds up to +10; extreme greed caps signals at HOLD |

16. Build candidate trades for symbols with score ≥ 60:
    - direction = LONG (v1; bearish setups reported as "avoid/short-watch", never auto-signaled)
    - entry = current close; stop = closest of (entry − 1.5×ATR(14), 20d swing low); TP1 = entry + 2×(entry−stop); TP2 = entry + 3×(entry−stop); R:R reported.
    - **cost-aware risk floor** (rule R6): the stop is widened if needed so that
      R ≥ max(0.75×ATR(14, daily), 15× the symbol's assumed round-trip cost).
      Tighter than that and the cost of trading eats the risk unit — at 0.5R of
      cost a 2R target needs a 66% win rate just to break even. The assumed cost
      is cited (`source_tool="cost_model"`), `floored` plans say so in their
      confluence notes, and the report prints cost-in-R + break-even win rate.
    - confluence notes auto-generated from components (e.g., "fresh MACD cross + price above SMA20/50").
17. Rank by score desc → top 3–5 `signals[]`.

### Phase 7.5 — Intraday Entry Refinement (30m/15m/5m/1m)

Applies to the final top signals only; the daily ranking and scoring preset
are never modified by this phase. For each signal:

a. Fetch intraday OHLCV per entry timeframe (default 30m/15m/5m/1m; windows
   30m→30d, 15m→14d, 5m→5d, 1m→2d — inside the yfinance caps). A missing
   timeframe is non-fatal: disclose and continue (R4).
b. Second sandbox pass computes per-timeframe features (close, EMA9/EMA21,
   RSI(14), MACD histogram, ATR(14), 20-bar swing low) — each a derived
   citation off that timeframe's last close. The intraday swing low is
   **context only**: 20 bars on 30m is ~10 hours, not the ~1 month the same
   lookback means on daily bars, so it no longer sets the stop.
c. Deterministic refinement (LONG-only):
   - **wait** — 15m bias bearish (close ≤ EMA21 and MACD hist not rising):
     the daily plan stands; the report says what to watch for.
   - **pullback** — 15m bullish but overextended (RSI(15m) ≥ 68 or
     RSI(5m) ≥ 72): entry = the 15m EMA21 zone; stop = entry − 1.5×ATR(15m).
   - **market** — 15m bullish, not overextended: entry unchanged; stop =
     tighter of (daily stop, entry − 1.5×ATR(30m)); 1m adds a micro-timing
     note (close vs EMA9).
   Then the stop is held at the **risk floor** of Phase 7: R may tighten only
   down to max(0.75×ATR(1d), 15× round-trip cost), i.e. by at most ~25%, and
   the plan is flagged `risk_floored` when the floor binds. TP1 = entry + 2R,
   TP2 = entry + 3R on the **refined** risk. Invariant: stop < entry < TP1 <
   TP2 always holds, and cost-in-R stays ≤ 1/15 (~0.07R).
d. Entry chart per signal (15m price + EMA21 + entry/stop/TP lines) streams
   to the UI "Charts analyzed" gallery; all levels render in the report's
   "Entry plans (intraday refinement)" section with their citations.

Roles fall back when a timeframe is missing (bias: 15m→30m→1h→5m→1m;
structure: 30m→15m→1h→5m; trigger: 5m→1m→15m→30m). Off-switches: Settings
toggle (`entry_refinement`), `--entry-tf off`, or `entry_timeframes: []` in
the API request; a custom set via `--entry-tf 30m,15m`.

### Phase 8 — Critique & Synthesis
18. Critic checks: every table cell cited? news claims dated? RSI>75 names flagged as overextended rather than signaled (reference case: ENA RSI 72.5 / SUI 68.4 handled as "strong but late momentum")? If gaps → re-run missing phase once.
19. Synthesizer emits `ScanReport`:

```json
{
  "market": "crypto", "as_of": "ISO-8601",
  "context_summary": "paragraph with web citations",
  "sentiment": [{"index": "fear_greed", "value": 46, "regime": "neutral", "cite": "…"}],
  "signals": [
    {"symbol": "XRPUSD", "direction": "LONG", "score": 82,
     "entry": 1.4755, "stop": 1.3520, "tp1": 1.7225, "tp2": 1.8460,
     "rr": "2.0R", "confluence": ["MACD bullish cross", "above SMA20/50", "CLARITY Act catalyst"],
     "citations": [ ... ]}
  ],
  "avoid": [{"symbol": "ENAUSD", "reason": "RSI 72.5 overextended; 138% annualized vol"}],
  "disclaimer": "Not financial advice…"
}
```
20. Persist run trace + report (+ signals table) locally; sync to Supabase if consent="on". Render in UI with collapsible phase cards mirroring this document.

**End-to-end budget:** ≤ 4 min cloud LLM · 28 tool calls typical (7 searches, 2 mover lists, 1 quotes, ~12 OHLCV, main sandbox pass) + entry refinement ≤ 20 intraday fetches + 1 sandbox pass for the top signals.

### Phase 9 — Signal ledger (measurement, not prediction)

Every emitted signal is appended to `<data_dir>/signals.jsonl` (module
`ledger.py`) with the levels actually acted on (refined plan when it is
actionable, otherwise daily), the risk unit, the assumed round-trip cost and
its cost-in-R, a sha256 of the OHLCV CSV it was derived from, the decision bar
date, a fingerprint of the scoring weights, and a `context` dict of
signal-time regime features (24h change, volume vs 30d average, RSI(14),
SMA20/SMA50 distance %, BTC 20d momentum for crypto — trial T1 enrichment,
frozen at decision time for later conditioning analyses). Emission is
deduplicated (trial T1): only the first signal per (market, symbol, decision
day) is recorded — the same idea re-emitted later the same day is one bet,
not a new one. Backfill imports history as-recorded, without dedupe. Demo-data
runs are recorded with `demo=true` so they can be excluded from statistics.

`signaldesk outcomes` resolves each record against subsequent daily bars with a
triple-barrier resolver (`outcomes.py`): TP1, TP2, stop, or the time barrier
(default 14 bars) mark-to-market. `signaldesk outcomes --backfill` imports
signals from past `runs/*/report.json` to get a historical sample immediately.
`signaldesk ledger` shows what is recorded.

Fixed conventions (rule R7 — never re-labelled after seeing results):
resolution starts on the first bar **after** the decision bar; a same-bar
stop/target tie resolves to the **stop**; the declared exit policy is 50% off at
TP1 with the remainder running to TP2 or the stop, and the stop is **not** moved
to break-even; censored signals are marked to market rather than dropped.

Statistics (`metrics.py`): expectancy in R net of cost with a week-block
bootstrap CI, hit rate with a Wilson interval, profit factor, median bars to
TP1, MFE/MAE, and breakdowns by entry mode / market / symbol. The CLI states
plainly when the sample is too small to conclude (fewer than ~4 independent
weeks, n < 100, or a mostly-censored sample).

---

## WF-2 · Ticker Deep Dive
Plan → company profile/financials/ratios/estimates/analyst research tools → earnings history/schedule → transcript analysis (v1.1) → peers comparison → news catalysts → one-page cited dossier (bull case / bear case / key levels from sandbox TA). When the technical score clears the threshold, the emitted signal gets the same Phase 7.5 intraday entry refinement as WF-1.

## WF-3 · Forex / Metals Scan
WF-1 with: Alpha Vantage FX daily/intraday adapters; DXY, yields (FRED macro snapshot) and Fed context weighted at 20% in scoring; ATR in pips; session-window note (London/NY) added to signals.

## WF-4 · Custom Watchlist Scan
WF-1 where universe = user's synced watchlist; per-asset mini-reports; "what changed since my last scan" diff section.

## WF-5 · Signal Outcome Tracker  *(shipped through roadmap item 28, 2026-09-28)*
Shipped: the signal ledger (Phase 9), the triple-barrier resolver,
`signaldesk outcomes` (expectancy in R net of cost, hit rate, profit factor,
time-to-TP1, MFE/MAE, breakdowns, with CIs and sample-size caveats), and the
item-28 remainder:

- **Scheduled daily resolution** — while the server runs, a background loop
  resolves the ledger once per local day (immediately on startup when today's
  pass is pending; a failed pass is retried hourly and disclosed). State:
  `<data_dir>/outcomes_state.json`; off-switch `SIGNALDESK_SCHEDULER=0`.
- **UI stats dashboard** — the Outcomes view ("last 30 signals: x% hit TP1",
  expectancy cards with CIs, breakdowns, per-signal status table). The view
  reads cached resolutions (`GET /api/outcomes` never fetches bars);
  resolution happens via the daily loop or the "Resolve now" button
  (`POST /api/outcomes/resolve`).
- **Paper execution log** — mark each open signal filled (with the real
  price) or missed, from the dashboard row or `signaldesk paper fill|miss|list`.
  Reports fill rate, direction-aware entry slippage in R, and crypto funding
  drag over resolved holds. Kept additive to the R7 accounting: outcomes keep
  resolving the modelled plan, the first fill/miss decision per signal counts,
  and the gap stats (execution-vs-model) never re-label a resolution.

One pipeline (`resolver.py`) serves the CLI, the API and the scheduler, so
there is exactly one place where resolution conventions live.

---

## Global Workflow Rules
- R1 Every numeric claim in any report carries a citation (direct or derived-with-formula).
- R2 Research before compute: web context phases never run after sandbox numbers are final (prevents narrative back-fitting).
- R3 Overextended filter: RSI ≥ 75 → cannot be emitted as LONG signal (listed under Avoid/Watch).
- R4 Degradation over failure: any unavailable phase is disclosed in the report header.
- R5 LLM never invents market numbers: critic rejects any figure not backed by artifact/sandbox output.
- R6 Cost-aware risk units: every signal's stop is widened, if needed, so R ≥ max(0.75×ATR(14, daily), 15× the assumed round-trip cost); the assumed cost is a cited assumption, cost-in-R and the break-even win rate are printed per signal, and `floored` plans are labelled. A trade whose cost is a large fraction of R is not a trade.
- R7 Fixed outcome accounting: the ledger/resolver conventions (next-bar start, pessimistic same-bar tie-break, declared 50/50 exit policy, no break-even stop move, censoring marked to market) are declared in `outcomes.py` and never re-labelled after seeing results. Statistics are reported with intervals and sample-size caveats, not point estimates alone.
