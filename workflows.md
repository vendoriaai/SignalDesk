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
4. Read the top hit: its article's opening text is fetched (Tavily extract when a key is set, else a direct page fetch with a 45 s total budget; demo scans skip this) and the claim quotes the article; the search snippet is the fallback. Claims go into `context_claims[]` with source URL — these become web citations.
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
14. Extract catalyst claims per asset → `catalysts[symbol][]` citations: the first hit's article text is fetched (same mechanism/budget as Phase 1; snippet stays the fallback), cited as `column="extracted_text"` when the page was read.
14b. **Regime gates** (item 32, trial T2): before a signal is emitted, the candidate must pass the declared gates — price above its 200d SMA, annualized vol <= 150%, 7d gain <= +50%, price <= 25% over SMA20; BTC below its 200d SMA caps the whole crypto book. Gated candidates move to `avoid[]` with the triggering value registered as a derived citation; <200d-history names are disclosed as unevaluated (R4).  *(Reference: SUI institutional staking/CME futures; TAO AI-narrative; AAVE RWA/news; ETF inflow headlines.)*

### Phase 7 — Scoring & Signal Construction
15. Score each symbol on **both sides** via `strategy/scoring.py` — the long
    preset `trend-momentum-v1` and its declared mirror
    `trend-momentum-short-v1` (same weights, bearish conditions):

| Component | Weight | Condition (bullish) | Condition (bearish mirror) |
|---|---|---|---|
| Trend stack | 40% | close>SMA20, close>SMA50, EMA9>EMA21 (13.3% each) | close<SMA20, close<SMA50, EMA9<EMA21 |
| Momentum | 25% | MACD hist > 0 (15%); hist increasing vs prior bar (10%) | MACD hist < 0 (15%); hist falling vs prior bar (10%) |
| RSI regime | 15% | 50≤RSI<70 full (15%); RSI 45–50 or 70–75 half; else 0 | 30<RSI≤50 full (15%); RSI 25–30 or 50–55 half; else 0 |
| Volume/lqi | 10% | volume ≥ 30d avg (10%) | volume ≥ 30d avg (10%) |
| Sentiment adj | 10% | market Fear&Greed neutral→greedy adds up to +10; extreme greed caps signals at HOLD | Fear&Greed neutral→fearful adds up to +10; extreme fear (≤20) caps short signals at HOLD |

    A missing/NaN SMA or EMA level awards no short trend leg ("cannot judge"
    scores 0, never bull). RSI ≤ 25 flags the short side overextended (falling
    knife — R3 mirror) and RSI ≥ 75 flags the long side (R3).

16. Build candidate trades for symbols whose **better side** clears its gates
    and scores ≥ 60 (one signal per symbol; an exact score tie goes LONG):
    - direction = the eligible side with the higher score (LONG default). The
      short side is refused by its mirrored gates exactly as longs are (item
      32 + mirror): above the 200d SMA, into a falling-knife 7d move (< −50%),
      extended more than 25% below SMA20, at the same 150% volatility
      extreme, and — book level — when BTC is above its own 200d SMA the
      whole crypto short book stands down. Refusals land in the avoid list,
      cited, prefixed with the refused side.
    - LONG: entry = current close; stop = closest of (entry − 1.5×ATR(14), 20d swing low); TP1 = entry + 2×(entry−stop); TP2 = entry + 3×(entry−stop).
    - SHORT (mirror): stop = closest of (entry + 1.5×ATR(14), 20d swing high); TP1 = entry − 2×(stop−entry); TP2 = entry − 3×(stop−entry). The ledger risk unit is |entry − stop| and stays positive; `direction` carries the orientation.
    - **cost-aware risk floor** (rule R6, both directions): the stop is widened if needed so that
      R ≥ max(0.75×ATR(14, daily), 15× the symbol's assumed round-trip cost).
      Tighter than that and the cost of trading eats the risk unit — at 0.5R of
      cost a 2R target needs a 66% win rate just to break even. The assumed cost
      is cited (`source_tool="cost_model"`), `floored` plans say so in their
      confluence notes, and the report prints cost-in-R + break-even win rate.
    - confluence notes auto-generated from components (e.g., "fresh MACD cross + price above SMA20/50").
17. Rank by score desc → top 3–5 `signals[]`.

### Phase 7.5 — Intraday Entry Refinement (30m/15m/5m/1m)

Applies to the final top **LONG** signals only (SHORT signals keep their daily
plan — the deterministic rules below are written for the long side; the skip
is disclosed in the report, R4, and SHORTs get the same chart ladder in
Phase 7.6 without a deterministic refinement). The daily ranking and scoring
preset are never modified by this phase. For each signal:

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
d. An entry chart (price + EMA21 + entry/stop/TP lines) for EVERY fetched
   timeframe — not just the primary — streams to the trace and the UI
   "Charts analyzed" gallery; all levels render in the report's
   "Entry plans (intraday refinement)" section with their citations.

Roles fall back when a timeframe is missing (bias: 15m→30m→1h→5m→1m;
structure: 30m→15m→1h→5m; trigger: 5m→1m→15m→30m). Off-switches: Settings
toggle (`entry_refinement`), `--entry-tf off`, or `entry_timeframes: []` in
the API request; a custom set via `--entry-tf 30m,15m`.

### Phase 6.5 — AI Signal Generation (the vision LLM decides)

When enabled (Settings `ai_signal_generation`, default on; non-demo; LLM creds
present), **the vision LLM generates the signals themselves** — the
deterministic preset steps aside. One vision call per scanned symbol, and every
call sees the full picture: the symbol's 1d + 1h TA charts (P4) plus a
full-context brief — its own indicator snapshot (close, 24h/3d/7d/30d change,
RSI14, MACD histogram + slope, SMA20/50/200 position, ATR14, annualized vol,
volume vs 30d avg, 20-bar range, Fear & Greed, altcoin season), the
deterministic policy gates for both directions as advisory notes, the
round-trip cost economics in R terms at the risk floor (cost-in-R and the
break-even win rate for a 2R target), the exact last 10 daily OHLCV bars, a
relative-strength line for every other scanned symbol, every collected
market-news claim as FULL article text (up to ~4 KB per article; the report
keeps a short lede), and the macro snapshot (DXY/US10Y for forex/metals). Strict-JSON reply: `direction`
(LONG/SHORT/NONE), `score` (0-100 conviction, ranked), `rationale`, optional
`invalidation` (the level that proves the trade wrong — it seeds the stop).

- Signals = the AI's picks with direction ≠ NONE and score ≥ the scan
  threshold, top 5 by score. The report header and ledger mark them
  (`ai-vision-v1`, `generator="ai_vision_v1"`, AI weights hash over the prompt
  version — currently `ai-vision-v3` — so outcomes stay separable, R7). First
  real application pre-registers trial `ai-signal-generation-v1` (frozen
  criteria, min 30 resolved signals).
- **Policy gates are advisory warnings on this path (operator choice)**: a
  regime/overextension/extremes violation is shown as a ⚠ confluence line with
  its citation and the scan discloses it — it does not refuse the AI's pick.
  The draft stop is still clamped to [max(0.75×ATR(1d), 15× cost), 3×ATR(1d)]
  and TPs stay 2R/3R (R6); Phase 7.5/7.6 refine as usual afterwards.
- Symbols the AI passed (NONE or below threshold) land in the avoid list with
  the AI's rationale ("AI passed: …"), so every exclusion is explained.
- Fallbacks (R4, disclosed): demo scans and no-key runs use the deterministic
  engine verbatim (hard gates); a failed per-symbol read skips that symbol;
  zero usable reads fall back to the deterministic engine for the whole scan.
- Trace: each decision streams as a 🧠 `P6.5` analysis event (direction, score,
  rationale, invalidation, model) before the signals appear.

### Phase 7.6 — AI Chart Read (vision entry selection)

Runs after 7.5, before Phase 8 emits the report: the trace shows every chosen
pair's charts and the AI's read BEFORE the signals appear. For every signal
(LONG and SHORT):

a. The full chart ladder is rendered and streamed as `chart` events: the 1d
   and 1h TA charts from Phase 4 plus an entry-style chart per fetched
   intraday timeframe (30m/15m/5m/1m; SHORTs fetch the same ladder).
b. One vision-capable LLM call per signal (`agent/vision.py`, LiteLLM, the
   user's configured provider/model): the ladder is sent as labelled images,
   daily → 1m, with the direction and draft plan plus the scan's market
   context (every collected news claim, the macro snapshot, the BTC regime)
   so the entry decision sees what the generation pass saw. The model returns
   strict JSON — per-timeframe trend reads, the chosen entry, an optional stop
   level, a rationale, a confidence — streamed into the trace as `analysis`
   (🧠) events before the report.
c. The AI chooses only the entry level. `scoring.reconcile_ai_entry` keeps
   the stop geometry deterministic (R6/R7): stop distance = the AI stop's
   distance clamped into [max(0.75×ATR(1d), 15× round-trip cost), 3×ATR(1d)]
   (the draft risk unit when no ATR exists); TPs stay 2R/3R. An implausible
   entry (non-finite or > 3×ATR(1d) from the draft) is rejected and the
   deterministic plan kept, disclosed (R4). Every asserted number is cited
   (R1); the read itself is a direct `llm_vision` citation.
d. Accounting: ledger records carry `entry_mode="ai_chart_v1"`; the first
   real (non-demo) application pre-registers trial `ai-chart-entry-v1`
   (criteria frozen at declaration, min 30 resolved signals, judged on the
   expectancy CI) — until it closes, the read is a declared experiment, not
   settled policy (R7).

Off-switch: Settings toggle (`ai_chart_entry`, default on). Degradation (R4):
demo scans skip the read; a missing LLM key, a non-vision model error, or an
unreadable reply falls back to the deterministic plan with a disclosure.

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
Plan → company profile/financials/ratios/estimates/analyst research tools → earnings history/schedule → transcript analysis (v1.1) → peers comparison → news catalysts → one-page cited dossier (bull case / bear case / key levels from sandbox TA). When the technical score clears the threshold, the emitted signal gets the same Phase 7.5 intraday entry refinement and Phase 7.6 AI chart read as WF-1.

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
- R3 Overextended filter: RSI ≥ 75 → cannot be emitted as LONG signal; RSI ≤ 25
  → cannot be emitted as SHORT signal (falling knife, R3 mirror). Either way the
  symbol is listed under Avoid/Watch with the refused side named.
- R4 Degradation over failure: any unavailable phase is disclosed in the report header.
- R5 LLM never invents market numbers: critic rejects any figure not backed by artifact/sandbox output.
- R6 Cost-aware risk units: every signal's stop is widened, if needed, so R ≥ max(0.75×ATR(14, daily), 15× the assumed round-trip cost); the assumed cost is a cited assumption, cost-in-R and the break-even win rate are printed per signal, and `floored` plans are labelled. A trade whose cost is a large fraction of R is not a trade.
- R7 Fixed outcome accounting: the ledger/resolver conventions (next-bar start, pessimistic same-bar tie-break, declared 50/50 exit policy, no break-even stop move, censoring marked to market) are declared in `outcomes.py` and never re-labelled after seeing results. Statistics are reported with intervals and sample-size caveats, not point estimates alone.
