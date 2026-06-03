# Final conclusion — is there a deployable edge in these price-action strategies?

**No.** After a full investigation (30-day → 1-year → entry-filter tightening → exit/RR →
cost model + selectivity), the verdict is definitive: a small *gross* edge exists, but it is
**destroyed by transaction costs**, and no selectivity subset recovers it. Do not deploy
these strategies as-is on 15m.

## The evidence chain
1. **30-day:** bt_sweep looked best (38% win-when-hit). → an artifact of one low-vol month.
2. **1-year (31 coins, ~74k signals):** no strategy robustly beats breakeven. All ~33% win
   at RR 2:1, direction ~49–50% (**coin-flip — no directional edge**). bt_sr never beats
   breakeven; bt_trend most consistent.
3. **Entry-filter tightening (v1 vs v2):** zero improvement. bt_trend2 was 2.2× more
   selective for the same 33%. Entry confluence is not where edge lives.
4. **Exit/RR:** a *trailing stop* lifts every strategy to a small **gross** positive
   (+0.05 to +0.08 R/trade). "Let winners run" works — trend winners run, sweep winners
   revert. This is the only mechanism that helped.
5. **Cost model (this step):** the gross edge does not survive fees.

## The killer table — net expectancy (R/trade), best exit (trailing)

| Cost scenario (round-trip) | bt_trend | bt_sweep | bt_trend2 | bt_sweep2 |
|---|---|---|---|---|
| zero (gross) | +0.076 | +0.046 | +0.066 | +0.051 |
| **maker 0.10%** | −0.165 | −0.095 | −0.156 | **−0.036** |
| **taker 0.30%** | −0.646 | −0.379 | −0.602 | −0.209 |

Even optimistic **maker** costs flip everything negative.

## Why — the structural reason (the important lesson)
Cost in R units = `cost_frac / stop_frac` (stop_frac = stop distance / price). On 15m the
stops are tight, so costs dominate:

| Stop distance | net @ taker (best ex.) | share of signals |
|---|---|---|
| <0.5% | −0.71 to −1.08R | 25% |
| 0.5–1% | −0.34 to −0.43R | 30% |
| 1–2% | −0.10 to −0.19R | 28% |
| >2% | −0.11 to **+0.02R** | 16% |

Median stop ≈ ~1% → taker cost ≈ 0.30%/1% = **0.30R/trade**, vs a gross edge of only
**~0.06R**. Costs are ~5× the edge. BTC is the extreme case: ~0.2% 15m stops → net **−0.88R**.

## Selectivity can't save it
- stop ≥ 2% AND conf ≥ 80 (taker): net **−0.072R** (gross only +0.032R), n=4,334.
- Best single pair (OP): net **−0.255R**. Every pair is net-negative.
- Confidence filtering barely moves it (conf≥80 → −0.36R vs all −0.42R).

Only **one** cell anywhere is net-positive after taker costs: bt_trend2, stops >2%
(+0.020R) — a 16%-of-a-sub-strategy sliver, far too thin to build on.

## Bottom line
These strategies have **no directional edge** (≈coin-flip) and a real but tiny exit edge
(~0.06R gross) that is structurally smaller than costs because 15m stops are tight. The
harness did its job: it proved there's nothing deployable here **before any capital was
risked** — which is the whole point of a signal-quality harness.

## The one structural lever left (a new experiment, not a tweak)
Cost in R shrinks as stops widen, so **higher-timeframe entries** (1h/4h instead of 15m →
3–5× wider stops → 3–5× lower cost-in-R, and far fewer trades) is the only change that could
flip the economics — *if* the gross edge survives at higher TF. This needs the driver made
timeframe-parameterized (entry on 1h/4h) and a fresh replay. Worth doing only if you want to
keep pursuing price-action signals; otherwise the honest read is that this signal family
doesn't carry a tradable edge on intraday crypto after costs.
