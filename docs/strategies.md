# Strategy authoring

Signals are produced by **versioned presets** in `signaldesk/strategy/scoring.py`.
Presets are plain Python — fork them, tune weights, compare results.

## Built-in: `trend-momentum-v1`

| Component | Weight | Bullish condition |
|---|---|---|
| Trend stack | 40% | close > SMA20, close > SMA50, EMA9 > EMA21 (⅓ each) |
| Momentum | 25% | MACD histogram > 0 (15), histogram rising (10) |
| RSI regime | 15% | 50–70 full, 45–50 / 70–75 half |
| Volume | 10% | volume ≥ 30-day average |
| Sentiment | 10% | Fear & Greed neutral → +5, greed → +10; ≥80 caps everything at HOLD |

Signal construction (score ≥ 60, emitted per side — see the short mirror below):
`stop = closest of (entry − 1.5×ATR, 20d swing low)` · `TP1 = entry + 2R` ·
`TP2 = entry + 3R` · RSI ≥ 75 is never signaled LONG (overextended rule R3).

The stop is then widened if needed to the **cost-aware risk floor** (rule R6):
`R ≥ max(0.75×ATR(1d), 15× the symbol's assumed round-trip cost)`. The assumed
cost per instrument lives in `signaldesk/costs.py` (fees + spread + slippage,
cited in reports as `source_tool="cost_model"`); the report prints each signal's
R, cost-in-R and the break-even win rate a 2R target needs just to cover cost,
and `floored` plans say so in their confluence notes. Rationale: a stop tighter
than the floor makes cost a large fraction of R — at 0.5R of cost a 2R target
needs a 66% win rate to break even.

## Built-in: `trend-momentum-short-v1` (the short mirror)

The same weights score the bearish case: below SMA20/SMA50, EMA9 < EMA21, MACD
histogram negative and falling, RSI 30–50 full (25–30 / 50–55 half), Fear &
Greed fear side adds up to +10 and ≤20 caps shorts at HOLD. Plans mirror:
`stop = closest of (entry + 1.5×ATR, 20d swing high)` · `TP1 = entry − 2R` ·
`TP2 = entry − 3R`. RSI ≤ 25 (falling knife) is never signaled SHORT — the R3
mirror — and the regime gates mirror too: shorts are refused above the 200d
SMA, into a falling-knife 7d move (< −50%), extended more than 25% below SMA20,
at the same 150% volatility extreme, and BTC above its own 200d SMA caps the
whole crypto short book. Each scan scores both sides and emits the better
eligible side, one signal per symbol; refused sides land in the avoid list
cited (`LONG refused: …` / `SHORT refused: …`).

## Built-in: `fx-momentum-v1` (forex / metals)

Same trend/momentum/RSI skeleton, no volume (spot FX), and a 20% macro
component driven by the DXY and US10Y 30-day changes. Direction-aware: rising
yields help USD-base pair longs (USDJPY), hurt USD-quote longs (EURUSD);
metals like falling yields.

## Intraday entry refinement (Phase 7.5)

After a preset ranks candidates, the **final top LONG signals** are re-checked
on intraday bars (default 30m/15m/5m/1m) to refine the entry — the ranking and
weights above are unchanged. SHORT signals keep their daily plan (the
deterministic rules are written for the long side); the report discloses the
skip:

| Mode | When | Plan |
|---|---|---|
| `market` | 15m bias bullish (close > EMA21, MACD hist positive or rising), not overextended | enter at the daily entry; stop = tighter of (daily stop, entry − 1.5×ATR30m) |
| `pullback` | 15m bullish but RSI(15m) ≥ 68 or RSI(5m) ≥ 72 | enter on a pullback to the 15m EMA21 zone; stop = entry − 1.5×ATR15m |
| `wait` | 15m bias bearish | daily plan stands; the report says what to watch for |

Structure stops are ATR-only: the 20-bar *intraday* swing low is a ~10-hour
level, not the ~1-month level the same lookback means on daily bars, so it stays
in the cited snapshot as context but no longer sets the stop.

Refinement may tighten the risk unit by at most ~25%: the stop is held at the
same cost-aware risk floor as the daily plan (`risk_floored` marks it). TP1/TP2
stay +2R/+3R on the refined risk. Every level carries a derived citation and a
15m entry chart (price + EMA21 + level lines) lands in the UI "Charts analyzed"
gallery. Disable via Settings, `--entry-tf off`, or `entry_timeframes: []` in
the API request.

## Measuring your preset (do this before tuning weights)

Every emitted signal is recorded in `~/.signaldesk/signals.jsonl` with its
levels, risk, assumed cost and a fingerprint of the weights that produced it.
`signaldesk outcomes` resolves those records against subsequent daily bars
(triple barrier: TP1 / TP2 / stop / time) and prints expectancy in R net of
cost with a week-block bootstrap confidence interval, hit rate with a Wilson
interval, profit factor, median bars-to-TP1 and MFE/MAE. `--backfill` imports
signals from past `runs/*/report.json` so you get a sample immediately.

The same statistics live in the desktop app's **Outcomes** view — while the
server runs, a background loop resolves open signals once per local day, and
each open signal can be paper-logged as *filled* (with the real price, to
measure entry slippage) or *missed* (to measure fill rate) — or logged from
the CLI with `signaldesk paper fill|miss|list`. Open signals also carry a
live mark-to-market (current price with P&L % and unrealized R, refreshed
hourly by the server loop and every 5 minutes by the dashboard) — a
snapshot of where the trade stands, not a barrier hit: TP/stop results
still confirm only on closed daily bars.

Two more guards keep that sample honest. The scan screens crypto movers picks
through declared liquidity floors ($5M 24h volume, $50M market cap — cited in
the report like the cost model), so untradeable names never emit signals; a
missing tape keeps the name, disclosed. And because today's top-volume names
are yesterday's survivors, run `signaldesk universe` occasionally: it
classifies every past run's universe against today's bars and reports how
much of your signal sample sits on no-longer-active names — `signaldesk
outcomes` attaches that caveat to the statistics until enough post-screen
signals accumulate.

Judge a preset change on that output, on the *same* signal set, with the
sample size caveats it prints (fewer than ~4 independent weeks or n < 100 cannot
distinguish a real improvement from noise) — not on the in-sample score. And
before you change anything: pre-register it. `signaldesk trial add` freezes
the hypothesis, the judging criteria and a minimum sample into
`trials.jsonl`; the outcome layer judges it when the minimum is met, and the
declared criteria are never edited afterwards. Each ledger record also
freezes its signal-time context (24h change, volume ratio, RSI, SMA
distances, BTC 20d momentum), so conditioning analyses ("do signals fired
after a pump do worse?") are possible without re-deriving history.

## Recommended sizing (what the report suggests)

Each signal carries a sizing recommendation: inverse-volatility account risk
in the 0.5–1% band (a median-volatility setup risks 0.75%), with the implied
notional shown so leverage is visible. The whole market's book is
cluster-capped at 3% total account risk — crypto signals run 0.6–0.9
correlated, so ten simultaneous longs are ~1.4 independent bets, not ten.
It is advice only: SignalDesk never places orders, and the outcome
statistics keep measuring the plan's own risk unit regardless of what you
stake.

## Writing your own preset

```python
# signaldesk/strategy/scoring.py
from .scoring import SymbolFeatures, ScoreBreakdown

def my_preset(f: SymbolFeatures, fng) -> ScoreBreakdown:
    bd = ScoreBreakdown(0, 0, 0, 0, 0, 0)
    ...
    return bd
```

Guidelines:
1. Keep the `ScoreBreakdown` contract (workflow renders + tests rely on it).
2. Mark RSI ≥ 75 with `overextended = True` — the critic enforces R3 for every preset.
3. Version the name (`my-preset-v1`) and bump it on every behavior change so
   past reports keep their scoring context.

## Testing a preset

```bash
python -c "
from signaldesk.strategy import scoring
f = scoring.SymbolFeatures(close=100, rsi14=62, atr14=4, swing_low_20=90,
    above_sma20=True, above_sma50=True, ema9_above_ema21=True,
    macd_hist=0.5, macd_hist_prev=0.2, volume=2e8, volume_avg30=1.5e8)
print(scoring.score_symbol(f, 60))
"
```
