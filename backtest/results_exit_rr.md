# Exit / RR lab — expectancy (R per trade), 1 year, 17 coins, 30,554 matured signals

Re-walked the seeded signals' forward 15m paths under 7 exit policies. 1R = |entry − sl|;
within-bar ties resolve stop-first (conservative); unresolved-in-72h marked to market.

| Policy | bt_trend | bt_sweep | bt_trend2 | bt_sweep2 |
|---|---|---|---|---|
| fix_1.5 | −0.015 | +0.018 | −0.004 | +0.023 |
| **fix_2.0** (baseline) | −0.000 | +0.025 | −0.001 | +0.037 |
| fix_2.5 | +0.017 | +0.018 | −0.010 | +0.033 |
| fix_3.0 | +0.028 | +0.016 | +0.013 | +0.038 |
| be@1→2 | −0.002 | +0.013 | −0.000 | +0.029 |
| be@1→3 | +0.019 | +0.012 | +0.011 | +0.027 |
| **trail1R** | **+0.076** | **+0.046** | **+0.066** | **+0.051** |

win% @2R: 33 / 34 / 33 / 36.  n: 8774 / 6579 / 3914 / 11287.

## Findings
1. **Trailing stop wins for every strategy** — the only policy that lifts all four clearly
   positive (+0.05 to +0.08 R/trade gross). This is "cut losers, let winners run": at 33%
   win the fixed 2:1 is break-even, but the winners' MFE has a fat right tail that a trail
   harvests.
2. **Match the exit to the strategy type:**
   - *Trend* (bt_trend/bt_trend2): expectancy rises monotonically with room —
     fix_2.0 ≈ 0 → fix_3.0 +0.028 → trail +0.076. Trend winners RUN; give them room.
   - *Sweep* (mean-reversion): fix_2.0 +0.025 > fix_3.0 +0.016 — sweep winners REVERT, so a
     nearer target is better than a far one; trailing still edges it (+0.046).
3. **Breakeven-at-1R does not help** (≈ flat / slightly worse) — moving to BE early clips
   the winners that drive the edge.
4. Entry-filter tightening (v2) is still a wash on expectancy too — bt_trend vs bt_trend2
   are within noise at every policy.

## ⚠ The honest caveat: this edge is thin vs costs
+0.05–0.08 R/trade is **gross, before fees/slippage/spread**. With ~8,000–11,000 trades/yr
across 17 pairs (~20–30/day) and crypto round-trip costs that can be a meaningful fraction
of 1R, transaction costs could **erase or invert** this edge. It is NOT yet a deployable
edge — it's a direction (trailing exits + the right exit per strategy type).

## Next levers (to turn gross edge into net edge)
1. **Add an explicit cost model** (fee + slippage per round trip in R) and find the
   break-even trade frequency — this decides whether anything here survives.
2. **Cut frequency / raise selectivity** so the per-trade edge clears costs (fewer, higher-
   conviction signals; trade only strong pairs / favorable regimes).
3. **Trail-parameter sweep** (activation 0.5/1/1.5R, trail 0.75/1/1.5R) — the trail was
   untuned; there may be a better setting, but beware overfitting.
