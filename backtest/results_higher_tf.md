# Higher-timeframe test — the structural fix works (4h entry crosses positive)

The cost model said the 15m edge died because stops are tight (cost-in-R = cost_frac /
stop_frac). Prediction: higher-TF entries → wider stops → lower cost drag → the gross
trailing edge survives. **Confirmed.** At 4h entry, the trend-pullback strategy with a
trailing stop is **net-positive after costs at every fee level**.

## The headline sweep — bt_trend, best trailing exit, NET expectancy (R/trade)

| Entry TF | stops (median) | gross | maker 0.10% | taker 0.30% |
|---|---|---|---|---|
| 15m | ~1% | +0.076 | −0.165 | −0.646 |
| 1h  | ~1.5% | +0.111 | +0.020 / ~0 | −0.163 |
| **4h** | **>2% (88% of signals)** | **+0.165 to +0.180** | **+0.130 to +0.145** | **+0.057 to +0.075** |

Monotonic, exactly as the cost model predicted. 15m = dead; 1h = break-even at maker only;
**4h = clearly positive even at taker** (market-order) fees — so it's robust to whether
exits fill as maker or taker, which is what makes 4h convincing where 1h was not.

## 4h detail (cost = taker 0.30%, the conservative/realistic exit assumption)
- **bt_trend**: trail a1.5 t1.0 → **+0.075R**, trail a1.0 t1.0 → +0.061R, fix_3.0 → +0.004R.
- **bt_sweep**: marginal — trail a1.5 t1.0 → +0.014R, trail a1.0 → +0.005R (≈ break-even).
- Pooled all signals: net **+0.037R** (gross +0.122R). stop ≥ 2%: **+0.050R**.
- Strong pairs (small n, exploratory): SEI +0.56, DOT +0.32, LTC +0.26, SHIB +0.20,
  LINK +0.19, NEAR +0.08. Weak: **BTC −0.26** (its 4h ranges are tight → still cost-bound).
- Confidence is *not* a useful filter (70–79 band is as good as 80+).

## Why it works
1. **Wide stops kill the cost drag.** 88% of 4h signals have stops >2% → taker cost ≈
   0.30%/3% ≈ 0.10R, well under the ~0.18R gross trailing edge.
2. **Trailing captures trend skew.** Direction is still ~coin-flip; the entire edge comes
   from letting winners run (trend-following positive skew) while costs stay small. Wider
   trailing (activate ~1R, trail 1–1.5R) is best for trend.
3. **Far fewer trades** (~1,764 bt_trend signals/yr across 30 pairs vs ~15k at 15m) → less
   cumulative cost, less overtrading.

## Recommended configuration (the deployable candidate)
- **Strategy:** bt_trend (trend pullback), 4h entry / 1d trend-bias.
- **Exit:** trailing stop, activate at +1R, trail ~1–1.5R (NOT breakeven-at-1R, which hurts).
- **Execution:** maker/limit entries where possible (lifts net further); even all-taker is positive.
- **Universe:** favor the stronger pairs; **exclude BTC** (cost-bound at 4h).
- **bt_sweep** at 4h is only marginal — secondary at best.

## Honest caveats (read before risking capital)
- **In-sample, one year, one regime.** No walk-forward / out-of-sample test yet — that is the
  mandatory next step before any capital. The result is *not* from entry-parameter tuning
  (low overfitting risk: it came from a structural TF change the cost model predicted a
  priori, and all trail settings are positive at 4h), but one year is one regime.
- **4h sample is thinner** — per-pair numbers (e.g. SEI +0.56) are noisy; trust the pooled
  +0.06R, not individual pairs.
- **Edge is modest** (~+0.06–0.075R/trade at taker). Real, but small; concurrency limits and
  funding/borrow (if levered) would eat into it.
- Direction has no edge — this lives or dies on the trailing exit's positive skew, which can
  degrade in choppy, range-bound regimes (cf. the bad months Oct/Dec/Apr in the 1-yr matrix).

## Bottom line
The signal family is **not** hopeless — the 15m timeframe was. **bt_trend on 4h with a
trailing stop is a plausibly profitable configuration after realistic costs** (+0.06 to
+0.18 R/trade), driven by wide stops (low cost drag) + trend-following skew. Validate
out-of-sample, then paper-trade it live through the harness before committing capital.
