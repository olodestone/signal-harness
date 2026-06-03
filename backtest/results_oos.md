# Out-of-sample validation — the 4h trailing edge HOLDS on an unseen year

Same exact config (4h entry, bt_trend/bt_sweep, trailing exits), tested on the **prior,
non-overlapping year** the strategy was *not* selected on:
- In-sample (IS): ~2025-06 → 2026-06 (where the edge was found)
- Out-of-sample (OOS): ~2024-06 → 2025-06 (29 pairs, 2,606 signals, all resolved)

## Net expectancy (R/trade) — IS vs OOS, trailing exits

**bt_trend**
| policy | IS taker | OOS taker | IS maker | OOS maker |
|---|---|---|---|---|
| trail a1.0 t1.0 | +0.061 | **+0.010** | +0.130 | +0.066 |
| trail a1.0 t1.5 | +0.075 | **+0.038** | +0.145 | +0.094 |
| trail a1.5 t1.0 | +0.057 | **+0.019** | +0.127 | +0.075 |

**bt_sweep**
| policy | IS taker | OOS taker | IS maker | OOS maker |
|---|---|---|---|---|
| trail a1.0 t1.0 | +0.005 | **+0.070** | +0.045 | +0.107 |
| trail a1.0 t1.5 | −0.001 | **+0.067** | +0.038 | +0.104 |

**Pooled (trail a1.0 t1.0, taker):** IS net **+0.037R** vs OOS net **+0.038R** — near-identical.
stop≥2%: IS +0.050R vs OOS +0.054R.

## Verdict: VALIDATED
- **bt_trend is net-positive at every cost level in BOTH years.** OOS taker is smaller
  (+0.01–0.04R) than IS but still positive; maker ~+0.07–0.09R both years.
- **bt_sweep is actually stronger OOS** (taker +0.07 vs +0.005 IS) — borderline in-sample,
  solidly positive in the prior year. Mean-reversion suited 2024–25 well.
- The **pooled net edge replicates almost exactly** (+0.037 → +0.038R). The sign, the
  mechanism (wide stops + trailing trend/skew), and the rough magnitude all hold on unseen
  data. **This is not a one-year artifact.**

## Important refinement: do NOT select pairs
Pair-level edges do **not** persist year-to-year:
- IS best: SEI / DOT / LTC / LINK / NEAR; IS worst: **BTC −0.26R**
- OOS best: ETH / OP / BNB; OOS worst: SUI / XRP — and **BTC flipped to +0.02R**

Per-pair n≈100 is noisy (e.g. the +2.456R outlier in a 1%-share bucket — ignore). The
*basket* edge is what replicates; cherry-picking pairs would be overfitting. **Trade the
broad basket, not a hand-picked pair list.** Confidence filtering also doesn't help in either
year.

## Final, validated configuration
- **bt_trend AND bt_sweep** (both validated; run both for diversification — trend is steadier
  across cost levels, sweep was stronger in the older year).
- **4h entry / 1d bias.**
- **Trailing stop**, activate ~1R, trail 1–1.5R (wide). Not breakeven-at-1R.
- **Maker/limit entries** materially improve net (roughly doubles it vs all-taker).
- **Whole basket**, no pair selection, no confidence gate.

## Honest residual caveats
- Edge is **modest**: ~+0.02–0.04R/trade (trend) to ~+0.07R (sweep) at taker; ~2× at maker.
  Real, repeatable, but small — sized risk, concurrency limits, and funding will erode it.
- Two adjacent years within one broad crypto era. Two non-overlapping years both positive is
  solid, but more regimes (a bear year, a different asset class) would strengthen it further.
- 4h sample is modest (~2.6–3k signals/yr); trust pooled numbers, not pair/month cells.
- Next step before capital: **paper-trade live through the harness** (it already logs +
  scores signals) for a few months to confirm execution/slippage assumptions hold.
