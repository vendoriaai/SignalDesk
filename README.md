<center>
  <img src="logo.svg" alt="SignalDesk Logo" width="120"/>
  <h1>SignalDesk</h1>
</center>

<center>

An open-source, cross-platform **AI market research & signal agent**: ask
"scan the crypto market and find the best pair to trade now" and get a ranked,
fully-cited signal report — entry, stop loss, take profits, R:R, confluence —
in minutes. Read-only by design: SignalDesk never touches a broker or places
trades.

![Version](https://img.shields.io/badge/version-v1.0.0-blue?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-blue?style=flat-square)
![Status](https://img.shields.io/badge/status-Phase%204%20complete-brightgreen?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey?style=flat-square)

</center>

**Status:** v1.0.0 — Phase 4 complete. Full agent (crypto WF-1, forex/metals
WF-3, deep dives WF-2, watchlists WF-4), planner + eval harness, desktop app
stack (FastAPI + React UI, SQLite, Supabase auth/sync, pywebview), PyInstaller
installers for Windows/macOS/Linux, onboarding wizard, update checks, and a
docs site. Since v1.0.0: OpenRouter BYOK + model override, chart analysis in
chat, intraday entry refinement (30m/15m/5m/1m, Phase 7.5), and the P0
measurement layer — a signal ledger, triple-barrier outcome resolver and
`signaldesk outcomes` (expectancy in R net of cost, with confidence intervals),
plus a cost-aware risk floor so stops cannot be tighter than trading costs
allow. Post-1.0 quality work is in [roadmap.md](roadmap.md) Phase 5.


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
history sidebar (offline), watchlist manager with one-click rescans, and
settings for LLM/search keys (OS keychain only), the cloud-consent toggle, and
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

1. **News context** (web search, before any numbers — rule R2)
2. **Universe**: majors + top gainers/losers, capped (default 12 symbols)
3. **Data pull**: quotes snapshot + ~6 months daily OHLCV per symbol
4. **Sandbox compute**: RSI, SMA/EMA stack, MACD + histogram, ATR, annualized
   volatility, 3/7/30d returns, 20d swing high/low — in a sub-process with an
   import whitelist and a 60 s timeout
5. **Sentiment**: Fear & Greed, Altcoin Season
6. **Catalysts**: per-candidate news research for the top scorers
7. **Scoring** (`trend-momentum-v1`): trend 40% · momentum 25% · RSI regime
   15% · volume 10% · sentiment 10%. LONG-only, RSI ≥ 75 excluded, extreme
   greed caps at HOLD. Stop = closest of entry − 1.5×ATR / 20d swing low,
   widened if needed to the **cost-aware risk floor**
   (max(0.75×ATR(1d), 15× the symbol's assumed round-trip cost)) so trading
   costs stay a small fraction of the risk unit; TP1 = +2R, TP2 = +3R. The
   report prints each signal's R, cost-in-R and the break-even win rate at TP1.
8. **Entry refinement** (Phase 7.5): the top signals are re-checked on
   intraday 30m/15m/5m/1m bars — enter now at market vs. wait for a pullback
   (15m EMA21 zone) vs. wait for LTF recovery. Stops may tighten only down to
   the same risk floor (~25% max), TP1/TP2 stay 2R/3R on the refined risk, and
   every level is its own derived citation. Disable via Settings or `--entry-tf off`.
9. **Critique**: citation coverage + overextended checks → `ScanReport`
   (Markdown + JSON).
10. **Ledger**: every emitted signal is appended to `~/.signaldesk/signals.jsonl`
    with its snapshot hash and rule fingerprint, so `signaldesk outcomes` can
    score it later (expectancy in R net of cost, with confidence intervals and
    explicit sample-size caveats).

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
