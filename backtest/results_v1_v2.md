# v1 vs v2 — does tighter entry filtering add edge? (1-year backtest)

**TL;DR: No.** Adding confluence filters (triple-timeframe alignment, strong-candle
requirement, S/R confluence, multi-touch liquidity + displacement) produced **no
win-rate improvement** over the v1 baselines. All four variants sit at ~33% win-when-hit
(= breakeven at RR 2:1) with ~47–50% direction accuracy (coin-flip). **The edge is not in
entry filtering.**

## Setup
- 1-year historical replay, **17 coins** (network attrition dropped 14 of 31 during fetch;
  fine for this question — v1 and v2 ran on the *identical* pair set, so the paired
  comparison is unbiased on the subset). ~30.7k signals, 30,538 resolved.
- Market entries, RR 2:1 fixed → **breakeven win-when-hit ≈ 33%**.
- v1 = `bt_trend`, `bt_sweep`; v2 = `bt_trend2` (more selective), `bt_sweep2`.

## Full-year aggregate

| Strategy | Signals | Win-when-hit | Dir@4h | vs its v1 |
|---|---|---|---|---|
| bt_sweep  (v1) | 6,619  | 33% | 50% | — |
| bt_trend2 (v2) | 3,947  | 33% | 47% | = (2.2× more selective, 0 gain) |
| bt_trend  (v1) | 8,863  | 33% | 48% | — |
| bt_sweep2 (v2) | 11,342 | 32% | 50% | slightly worse |

`bt_trend2` cut signal count by ~55% via stricter gates and got the **exact same 33%** —
the removed signals were no worse than the kept ones. Tightening just trades sample for
nothing.

Cross-check: in the earlier 31-pair run, bt_trend and bt_sweep were also 33% → the 17-pair
subset did not distort the v1 numbers, so this comparison is trustworthy.

## Month-by-month win-when-hit % (`*` = best; breakeven ≈ 33%)

| Month | bt_trend | bt_trend2 | bt_sweep | bt_sweep2 |
|---|---|---|---|---|
| 2025-06 | 33 | 33 | **38\*** | 35 |
| 2025-07 | **36\*** | 36 | 33 | 35 |
| 2025-08 | 37 | **41\*** | 34 | 30 |
| 2025-09 | **39\*** | 37 | 29 | 29 |
| 2025-10 | 28 | **33\*** | 33 | 31 |
| 2025-11 | 37 | 35 | **37\*** | 33 |
| 2025-12 | 27 | 28 | **31\*** | 29 |
| 2026-01 | **37\*** | 35 | 34 | 33 |
| 2026-02 | 35 | 32 | 37 | **39\*** |
| 2026-03 | 36 | **37\*** | 33 | 31 |
| 2026-04 | 24 | 25 | 28 | **30\*** |
| 2026-05 | 30 | 29 | **38\*** | 33 |

No consistent v2 edge — v2 beats v1 in some months, loses in others (a wash). Months won:
bt_trend 3, bt_trend2 3, bt_sweep 4, bt_sweep2 2.

## What the data actually says
1. **Stop tuning entry filters** — across two year-runs and four variants the answer is a
   flat ~33% win / ~50% direction. Entry confluence isn't where edge lives here.
2. **bt_sweep has a mild, real low-vol/range edge**: 37–38% in Jun-2025, Nov-2025, May-2026
   (calm, ranging months). It only "led + beat breakeven" in 3/13 months though.
3. **Whole-market bad months**: Oct-2025, Dec-2025, Apr-2026 were sub-breakeven for
   *everything* (whipsaw/trending-against-mean-reversion). A regime filter that sits these
   out would help more than any entry tweak.

## Where edge could actually come from (next tests, by expected value)
1. **Exit / RR side** (highest value): at 33% win you need RR > 2 to profit. Test letting
   winners run past 2R (trailing stop / partial TP) to lift average-R-per-win, and/or an
   RR sweep (1.5 / 2.5 / 3.0).
2. **Regime filter**: trade bt_sweep only in low-vol/ranging conditions; flatten size in
   trending/whipsaw months.
3. **Pair selection**: restrict to each strategy's historically strong pairs.

Entry-filter tightening (this experiment) is a dead end — documented so we don't repeat it.
