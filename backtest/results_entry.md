# Refined-entry experiment — does a "better" entry beat the 4h-close? NO.

Same 4h setups, same structural stop, same 1h trailing exit — only the entry differs.
Fair metric = **net R per opportunity** (unfilled setups count as 0). Tested IS + OOS.

## Net R per opportunity (the fair comparison)

| | fill% | gross/fill | **net/opp taker** | **net/opp maker** |
|---|---|---|---|---|
| **IN-SAMPLE (2025-06→2026-06), all** | | | | |
| baseline (4h close) | 100% | **+0.106** | **+0.020** | **+0.077** |
| optA (limit @50% retrace) | 29% | −0.019 | −0.072 | −0.028 |
| optB (1h confirmation) | 83% | +0.063 | −0.075 | +0.010 |
| **OUT-OF-SAMPLE (2024-06→2025-06), all** | | | | |
| baseline | 100% | **+0.084** | **+0.013** | **+0.060** |
| optA | 28% | −0.034 | −0.063 | −0.027 |
| optB | 83% | +0.050 | −0.067 | +0.006 |

**Baseline wins on net/opportunity in both years, at both cost levels.** Holds per-strategy
too (bt_trend and bt_sweep individually).

## Why the "better" entries are worse — the real lessons
1. **Adverse selection on limit entries (optA).** It fills only ~17–37% — and the trades it
   *does* fill are the *worse* ones (gross/fill −0.019 vs baseline +0.106 in-sample). Setups
   that retrace to your limit are disproportionately the ones that then fail; the winners run
   away without ever filling you. Classic "limits catch the losers, miss the runners."
2. **The edge is in the runners, and the exit captures it.** With a trailing stop, the money
   is in the trades that take off immediately. Both A (misses them) and B (enters late, after
   the move) systematically clip that tail.
3. **Refining the entry tightens the stop → reinflates cost drag.** Entering closer to the
   stop shrinks stop_frac, so cost_in_R rises — the exact mechanism that killed 15m. A & B
   pay this tax for no benefit.
4. **optB enters at a worse price.** Waiting for 1h confirmation means entering later/higher
   on a long → lower gross/fill (+0.063 vs +0.106) for the "confirmation."

## Verdict — keep the baseline 4h-close entry
The blunt market-entry-at-4h-close **is** the good entry for this strategy, because the edge
is *exit-driven* (let winners run), not entry-driven. Trying to get a precise entry actively
destroys it. This also re-confirms the earlier OOS result: baseline net/opp is +0.020 (IS) /
+0.013 (OOS) taker — consistent and positive.

**No change to the deployed config.** strategy.py already enters at the 4h close — it's the winner.

(Params tested: retrace f=0.5, expiry 8h, trail a1.0 t1.0. A different retrace/expiry could
shift A/B at the margin, but the gap is large and the adverse-selection logic is structural —
not worth chasing.)
