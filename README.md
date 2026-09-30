<div align="center">

<img src="logo.ico" alt="SignalDesk Logo" width="120"/>

# SignalDesk

</div>

<div align="center">

An open-source, cross-platform **AI market research & signal agent**: ask
"scan the crypto market and find the best pair to trade now" and get a ranked,
fully-cited signal report — entry, stop loss, take profits, R:R, confluence —
in minutes. Read-only by design: SignalDesk never touches a broker or places
trades.

![Version](https://img.shields.io/badge/version-v1.0.0-blue?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-blue?style=flat-square)
![Status](https://img.shields.io/badge/status-Phase%205%20in%20progress-brightgreen?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey?style=flat-square)

</div>

**Status:** v1.0.0 — Phase 4 complete. Full agent (crypto WF-1, forex/metals
WF-3, deep dives WF-2, watchlists WF-4), planner + eval harness, desktop app
stack (FastAPI + React UI, SQLite, Supabase auth/sync, pywebview), PyInstaller
installers for Windows/macOS/Linux, onboarding wizard, update checks, and a
docs site. Since v1.0.0: OpenRouter BYOK + model override, chart analysis in
chat, intraday entry refinement (30m/15m/5m/1m, Phase 7.5) + the AI chart
read (Phase 7.6: the trace shows every chosen pair's 1d→1m chart ladder and
a vision LLM picks the entry, stop distance still risk-floored), the P0
measurement layer — a signal ledger, triple-barrier outcome resolver and
`signaldesk outcomes` (expectancy in R net of cost, with confidence intervals),
plus a cost-aware risk floor so stops cannot be tighter than trading costs
allow, and inverse-volatility position-sizing advice with a per-market
cluster cap (item 30) — the WF-5 remainder (roadmap item 28): a scheduled daily resolver,
the Outcomes stats dashboard in the UI, and a paper execution log for the
execution-vs-model gap — and universe integrity (item 29): a liquidity
screen on movers picks plus `signaldesk universe`, a point-in-time audit
that sizes the survivorship bias in the outcome sample. Post-1.0 quality
work continues in [roadmap.md](roadmap.md) Phase 5.


## Run the app

```bash
pip install -e .                 # python >= 3.11
signaldesk serve                 # browser UI at http://127.0.0.1:8787
# or: signaldesk desktop         # native window via pywebview (needs: pip install -e ".[desktop]")
```

On Windows with a user-site pip install (`pip install -e .` without admin),
the `signaldesk` launcher may not be on your shell's PATH. Invoke the module
directly instead:

```bash
python -m signaldesk.cli serve   # PATH-independent (works from the checkout)
```

or use the full launcher path:

```bash
"$APPDATA/Python/Python313/Scripts/signaldesk.exe" serve    # Git Bash
# cmd/PowerShell: %APPDATA%\Python\Python313\Scripts\signaldesk.exe serve
```

The UI gives you the chat agent (live execution trace + rendered cited report),
history sidebar (offline), watchlist manager with one-click rescans, the
Outcomes dashboard (expectancy in R with confidence intervals, hit rate,
profit factor, per-signal status, paper fill/miss logging — while the server
runs, open signals resolve automatically once per local day), and settings
for LLM/search keys (OS keychain only), the cloud-consent toggle, and
"delete my cloud data". A fresh install works fully without any cloud account.

Spec docs: [prd.md](prd.md) · [tad.md](tad.md) · [workflows.md](workflows.md) ·
[roadmap.md](roadmap.md) · [data-model.sql](data-model.sql)

## Quickstart (CLI)

```bash
pip install -e .          # python >= 3.11

# scans (WF-1 / WF-3):
signaldesk scan crypto               # live: yfinance + CoinGecko + alternative.me
signaldesk scan forex                # FX majors, macro from FRED/DXY/10Y
signaldesk scan metals               # gold & silver
signaldesk scan crypto --demo        # offline, deterministic, no keys

# deep dive on one ticker (WF-2):
signaldesk deepdive AAPL             # TA + fundamentals + EDGAR + earnings + news

# watchlists (WF-4):
signaldesk watchlist add growth BTCUSD ETHUSD SOLUSD --market crypto
signaldesk scan watchlist:growth     # includes "what changed" diff vs last scan

# natural-language entry point (planner: rules first, LLM BYOK if key set):
signaldesk ask "deep-dive NVDA before earnings"
signaldesk ask "scan forex"

# measurement: score recorded signals against subsequent bars
signaldesk ledger                    # what the ledger holds (runs, modes, rule fingerprints)
signaldesk outcomes --backfill       # import past runs, resolve them, print R statistics
signaldesk outcomes --horizon 7      # shorter time barrier (default 14 daily bars)
signaldesk paper fill <id> -p 101.2  # paper log: signal taken at 101.2 (entry-gap measurement)
signaldesk paper miss <id>           # paper log: signal skipped / never filled
signaldesk paper list                # fill rate, entry slippage in R
signaldesk universe                  # survivorship audit of every past run's universe
signaldesk trial add                 # pre-register a rule change (hypothesis + judging criteria)
signaldesk trial list                # the trial log (running and closed trials)
signaldesk learn train               # fit the meta-label model on ledger x outcomes (needs ~30+ resolved)
signaldesk learn report              # does the model's score separate wins from losses? (shadow mode)

# inspect the agent's execution trace:
signaldesk scan crypto --trace       # JSONL events
```

Optional keys in `.env` (see [.env.example](.env.example)): `TAVILY_API_KEY`
(sharper web research), `FRED_API_KEY` (macro; falls back to yfinance proxies),
`ALPHAVANTAGE_API_KEY` (forex OHLCV; falls back to yfinance),
`OPENAI_API_KEY`/`ANTHROPIC_API_KEY` (LLM planner),
`SUPABASE_URL`/`SUPABASE_ANON_KEY` (auth + cloud history sync). Everything
degrades with a disclosure in the report when a provider is missing (rule R4).

Each run writes `~/.signaldesk/runs/<id>/` with `report.md`, `report.json`,
`trace.jsonl` and every CSV artifact the report cites. API/app runs also persist
to `~/.signaldesk/signaldesk.db` (SQLite).

## How a signal is produced

1. **News context** (web search, before any numbers — rule R2): the top
   hit's article text is fetched where possible (snippet as fallback), so
   context lines quote the page itself
2. **Universe**: majors + top gainers/losers, capped (default 12 symbols)
3. **Data pull**: quotes snapshot + ~6 months daily OHLCV per symbol
4. **Sandbox compute**: RSI, SMA/EMA stack, MACD + histogram, ATR, annualized
   volatility, 3/7/30d returns, 20d swing high/low, 200d SMA — in a
   sub-process with an import whitelist and a 60 s timeout
5. **Sentiment**: Fear & Greed; Altcoin Season computed CoinGecko-native
   (share of the top-50 alts outperforming BTC over 30d)
6. **Catalysts**: per-candidate news research for the top scorers, quoting
   the linked article's opening text where the page was fetchable
7. **Signal generation** (Phase 6.5): on live scans the ranked signals
   themselves are generated by the vision LLM — one call per scanned symbol
   over its daily + 1h charts and a data brief (features, sentiment, BTC
   regime, news) returns direction, a 0-100 conviction score and a rationale;
   the top 5 become the signals. Policy gates appear as warnings on AI
   signals, not vetoes; draft stops stay risk-floored and TPs stay 2R/3R;
   the ledger marks the AI era (`generator="ai_vision_v1"` + trial
   `ai-signal-generation-v1`). Demo/no-key scans fall back to the
   deterministic preset below, disclosed.
8. **Scoring** (`trend-momentum-v1` + mirrored `trend-momentum-short-v1`):
   trend 40% · momentum 25% · RSI regime 15% · volume 10% · sentiment 10% —
   scored on **both sides** per symbol, and the better eligible side is
   emitted. RSI ≥ 75 excluded from LONGs, RSI ≤ 25 (falling knife) from
   SHORTs; extreme greed caps longs, extreme fear caps shorts at HOLD.
   LONG: stop = closest of entry − 1.5×ATR / 20d swing low; SHORT mirrors it
   (stop above entry, targets below). Stops are widened if needed to the
   **cost-aware risk floor**
   (max(0.75×ATR(1d), 15× the symbol's assumed round-trip cost)) so trading
   costs stay a small fraction of the risk unit; TP1 = ±2R, TP2 = ±3R. The
   report prints each signal's R, cost-in-R and the break-even win rate at
   TP1. Short-side regime gates mirror the long ones (refused above the 200d
   SMA, into falling-knife moves, extended below SMA20; BTC above its own
   200d SMA caps the crypto short book) — refusals land in the avoid list,
   cited.
9. **Entry refinement** (Phase 7.5): the top signals are re-checked on
   intraday 30m/15m/5m/1m bars — enter now at market vs. wait for a pullback
   (15m EMA21 zone) vs. wait for LTF recovery. Stops may tighten only down to
   the same risk floor (~25% max), TP1/TP2 stay 2R/3R on the refined risk, and
   every level is its own derived citation. Disable via Settings or `--entry-tf off`.
10. **AI chart read** (Phase 7.6): the execution trace streams every chosen
    pair's charts — daily down to 1m — before the signals appear, then a
    vision-capable LLM (your configured provider/model) reads the full
    ladder and picks the entry. The AI chooses only the entry level; the
    stop distance stays clamped to the cost-aware risk floor and TPs stay
    2R/3R, the read is cited and streamed (🧠 events), the ledger records
    `entry_mode="ai_chart_v1"`, and adoption is gated by pre-registered
    trial `ai-chart-entry-v1`. Disable via Settings (`ai_chart_entry`).
11. **Position sizing** (item 30, advice only): each signal carries a
    recommended account risk — inverse-volatility in the 0.5-1% band
    (median-volatility signal = 0.75%), cluster-capped at 3% total per
    market (correlated positions are fewer independent bets, not ten) — and
    the implied notional so leverage is visible. SignalDesk never places orders.
12. **Critique**: citation coverage + overextended checks → `ScanReport`
    (Markdown + JSON).
13. **Ledger**: every emitted signal is appended to `~/.signaldesk/signals.jsonl`
    with its snapshot hash and rule fingerprint, so `signaldesk outcomes` can
    score it later (expectancy in R net of cost, with confidence intervals and
    explicit sample-size caveats).
14. **Outcome tracking** (WF-5): while the app runs, a background loop resolves
    open signals once per local day against fresh daily bars; the Outcomes
    dashboard shows expectancy/hit-rate/profit-factor with intervals, and each
    open signal can be paper-logged as filled (real price → entry-slippage
    measurement) or missed (fill-rate measurement). Open signals also carry a
    live mark-to-market — current price with P&L % and unrealized R from the
    latest quote, refreshed hourly by the server loop and every 5 minutes by
    the dashboard — so "is this in profit yet?" is answered automatically
    between resolution passes.
15. **Universe integrity** (item 29): movers picks pass a liquidity screen
    (declared crypto floors — $5M 24h volume, $50M market cap — cited in the
    report; a missing tape keeps the name, disclosed), and `signaldesk
    universe` audits every past run's universe against today's bars to size
    the survivorship bias in the outcome sample (that caveat attaches to the
    statistics once an audit has been run).

Every number in the report is a citation: direct (tool, file, row, column) or
derived (formula + inputs). See [workflows.md](workflows.md) for the full spec.

## Development

```bash
pip install -e ".[dev]"
pytest                              # unit + workflow + API + sync tests (demo markets, mocked HTTP)
python -m signaldesk.evals.runner   # 30-case planner/workflow eval harness
signaldesk outcomes --backfill      # resolve recorded signals; expectancy in R (needs network)
cd ui && npm install && npm run build   # build the UI into signaldesk/ui/dist
ruff check .
```

If `import signaldesk` fails from outside the project directory, the editable
install is stale (e.g. the folder was renamed) — re-run `pip install -e .`;
the sandboxed compute children import the installed package, not the CWD.

## Safety

All exchange/data calls are read-only. The tool registry rejects any
write-side plugin. Every report carries a "not financial advice" disclaimer;
signals are heuristic scores, never promises of return. Raw expectancy numbers
from `signaldesk outcomes` are measurements of the system's own past signals —
they are not a forecast, and small samples are labelled as such.
