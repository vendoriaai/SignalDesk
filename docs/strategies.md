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

Signal construction (score ≥ 60, LONG-only):
`stop = closest of (entry − 1.5×ATR, 20d swing low)` · `TP1 = entry + 2R` ·
`TP2 = entry + 3R` · RSI ≥ 75 is never signaled (overextended rule R3).

The stop is then widened if needed to the **cost-aware risk floor** (rule R6):
`R ≥ max(0.75×ATR(1d), 15× the symbol's assumed round-trip cost)`. The assumed
cost per instrument lives in `signaldesk/costs.py` (fees + spread + slippage,
cited in reports as `source_tool="cost_model"`); the report prints each signal's
R, cost-in-R and the break-even win rate a 2R target needs just to cover cost,
and `floored` plans say so in their confluence notes. Rationale: a stop tighter
than the floor makes cost a large fraction of R — at 0.5R of cost a 2R target
needs a 66% win rate to break even.

## Built-in: `fx-momentum-v1` (forex / metals)

Same trend/momentum/RSI skeleton, no volume (spot FX), and a 20% macro
component driven by the DXY and US10Y 30-day changes. Direction-aware: rising
yields help USD-base pair longs (USDJPY), hurt USD-quote longs (EURUSD);
metals like falling yields.

## Intraday entry refinement (Phase 7.5)

After a preset ranks candidates, the **final top signals** are re-checked on
intraday bars (default 30m/15m/5m/1m) to refine the entry — the ranking and
weights above are unchanged:

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
the CLI with `signaldesk paper fill|miss|list`.

Judge a preset change on that output, on the *same* signal set, with the sample
size caveats it prints (fewer than ~4 independent weeks or n < 100 cannot
distinguish a real improvement from noise) — not on the in-sample score.

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
