# Regime-filter experiment — does skipping choppy regimes help? NOT robustly. Don't add it.

Tested causal chop filters (ADX(14), Kaufman ER(14) on the 1d frame) on the validated config
(bt_trend+bt_sweep, 4h-close, 1h trailing, cost-adjusted), IN-SAMPLE and OUT-OF-SAMPLE. The
filter is chosen by whether the SAME threshold helps BOTH windows — not the best per window
(that's the overfitting trap).

## Net per kept trade (taker), same threshold across windows

| filter | IS | OOS | kept% |
|---|---|---|---|
| none | +0.024 | +0.001 | 100% |
| ADX≥18 | +0.044 | −0.001 | ~78% |
| ADX≥22 | +0.037 | +0.001 | ~62% |
| ADX≥25 | +0.044 | −0.008 | ~50% |
| ER≥0.25 | +0.013 | +0.027 | ~45% |
| ER≥0.3 | +0.010 | −0.001 | 36% |
| ER≥0.4 | +0.051 | +0.017 | 22% |

## Findings
1. **ADX is overfit.** Strong in-sample (+0.044 vs +0.024), but ~zero/negative out-of-sample
   — no better than no filter. The OOS test caught a filter that looks great in-sample and
   is actually noise. (Methodological win even though the lever failed.)
2. **ER≥0.25 is inconsistent** — worse than no-filter IS, best OOS. Untrustworthy.
3. **ER≥0.4 is the only one positive in both windows** (+0.051 IS / +0.017 OOS), but:
   - discards **78% of trades** for a thin OOS gain (+0.016R),
   - **does not cleanly skip the bad months** — in-sample it made good months *worse*
     (Oct +0.63→−0.63, Jun +0.03→−0.53, Aug −0.15→−0.76) while helping others. It reshuffles
     variance rather than avoiding chop.
4. Per-strategy is inconsistent across windows (e.g. bt_sweep: ER hurts IS, helps OOS).

## Why it fails
Whether the next 72h will whipsaw is mostly knowable only in hindsight. A causal signal-time
chop metric can't reliably predict it, so it can't robustly skip the bad stretches. The
bad-month drag is largely **irreducible variance**, not a filterable signal.

## Verdict
**Do NOT add a regime filter.** Keep the validated config as-is (net-positive both years
unfiltered). Any of these filters would be fitting to noise — the opposite of what survived
every prior test. Absorb the bad months via the long-run positive average and risk sizing.
