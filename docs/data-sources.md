# Data sources

SignalDesk prefers **key-free public sources**, and every provider is optional:
when a feed is missing or down, the affected phase degrades with a disclosure
in the report header instead of failing the run (rule R4).

## Key-free (work out of the box)

| Source | Used for | Notes |
|---|---|---|
| yfinance | Quotes + OHLCV (stocks, crypto, FX `=X`, metals `GC=F`/`SI=F`) | One batched daily download per scan; intraday entry windows 30m→30d, 15m→14d, 5m→5d, 1m→2d (yfinance caps: ≤60d for 5m–30m, ≤7d for 1m) |
| CoinGecko | Crypto gainers/losers (universe discovery) | `coins/markets` endpoint |
| alternative.me | Crypto Fear & Greed Index | `/fng/` |
| blockchaincenter | Altcoin Season Index | Scraper feed; unstable → disclosed when down |
| SEC EDGAR | Annual revenue / net income (deep dives) | Declared User-Agent, rate-limited |
| DuckDuckGo | Web/news search fallback | needs `pip install "signaldesk[search]"` |

## Free key optional (higher quality / rate limits)

Set these in the UI Settings or `.env`; all live in your **OS keychain**.

| Key | Unlocks | Get one |
|---|---|---|
| `TAVILY_API_KEY` | Curated web research & per-asset catalysts | tavily.com (1k calls/mo free) |
| `FRED_API_KEY` | DXY + US 10Y macro snapshot (stock/FX/metals scans) | fred.stlouisfed.org |
| `ALPHAVANTAGE_API_KEY` | Forex daily OHLCV fallback | alphavantage.co |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM planner refinement (rules run without) | platform.openai.com / console.anthropic.com |

## Cloud (optional)

| Config | Purpose |
|---|---|
| `SUPABASE_URL` + `SUPABASE_ANON_KEY`, then email signup in Settings | History sync across machines, consent-gated analytics |

## Rate limits

Each adapter enforces a token bucket (e.g. Alpha Vantage 25/day free,
CoinGecko ~30/min). Scans cap the universe at 12 symbols by default
(`-n` up to 25) to stay inside every free tier.
