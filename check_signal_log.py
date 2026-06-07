"""
Read-only diagnostic for signal_log — confirms rows are persisting and shows
exactly how close the guardrail is to having a 72h-matured sample.

Run on Railway (DATABASE_URL is injected there):
    railway run python3 check_signal_log.py

Or locally with the prod URL exported:
    DATABASE_URL=postgres://... RUN_TAG=v1 python3 check_signal_log.py

Touches nothing — SELECTs only. Safe to delete afterwards.
"""
from __future__ import annotations

import config
import guardrail
import performance as perf

RUN_TAG = config.RUN_TAG
PAPER = int(config.PAPER_TRADING)
MIN_TRADES = guardrail.MIN_TRADES
WINDOW = guardrail.WINDOW

print(f"RUN_TAG={RUN_TAG!r}  PAPER_TRADING={config.PAPER_TRADING} (paper={PAPER})")
print(f"guardrail thresholds: MIN_TRADES={MIN_TRADES}  WINDOW={WINDOW}\n")

with perf._cur() as cur:
    # 1) Is anything landing at all?
    cur.execute("SELECT COUNT(*) AS n FROM signal_log")
    print(f"signal_log total rows: {cur.fetchone()['n']}")

    # 2) Breakdown by run_tag / paper — confirms new rows are tagged 'v1'
    cur.execute(
        "SELECT run_tag, paper, COUNT(*) AS n FROM signal_log "
        "GROUP BY run_tag, paper ORDER BY n DESC"
    )
    print("\nby run_tag / paper:")
    for r in cur.fetchall():
        print(f"  run_tag={r['run_tag']!r:8} paper={r['paper']}  n={r['n']}")

    # 3) Time bounds + server clock for the active slice
    cur.execute(
        "SELECT COUNT(*) AS n, MIN(signal_time) AS first, MAX(signal_time) AS last "
        "FROM signal_log WHERE run_tag=%s AND paper=%s",
        (RUN_TAG, PAPER),
    )
    row = cur.fetchone()
    cur.execute("SELECT NOW() AS now, current_setting('TimeZone') AS tz")
    clock = cur.fetchone()
    print(f"\nactive slice (run_tag={RUN_TAG!r}, paper={PAPER}):")
    print(f"  rows={row['n']}  first={row['first']}  last={row['last']}")
    print(f"  db NOW()={clock['now']}  db TimeZone={clock['tz']}")

    # 4) The guardrail's EXACT maturity gate
    cur.execute(
        "SELECT COUNT(*) AS n FROM signal_log "
        "WHERE run_tag=%s AND paper=%s "
        "AND signal_time::timestamp <= NOW() - INTERVAL '72 hours'",
        (RUN_TAG, PAPER),
    )
    matured = cur.fetchone()["n"]
    print(f"\nguardrail-matured (>72h) rows: {matured}/{MIN_TRADES}")
    if matured < MIN_TRADES:
        cur.execute(
            "SELECT MIN(signal_time) AS oldest_unmatured FROM signal_log "
            "WHERE run_tag=%s AND paper=%s",
            (RUN_TAG, PAPER),
        )
        oldest = cur.fetchone()["oldest_unmatured"]
        print(f"  -> still warming up. Oldest signal: {oldest}")
        print(f"  -> first signals cross 72h at: oldest + 72h")
        print(f"  -> expect a real 'n=.. mean=..R' line once {MIN_TRADES} rows pass 72h")
    else:
        print("  -> threshold met: guardrail should now print real statistics.")

    # 5) Eyeball the most recent few rows
    cur.execute(
        "SELECT signal_time, pair, direction, confidence, stage, run_tag "
        "FROM signal_log WHERE run_tag=%s AND paper=%s "
        "ORDER BY signal_time DESC LIMIT 5",
        (RUN_TAG, PAPER),
    )
    print("\nmost recent 5 rows:")
    for r in cur.fetchall():
        print(f"  {r['signal_time']}  {r['pair']:12} {r['direction']:4} "
              f"conf={r['confidence']} stage={r['stage']} tag={r['run_tag']}")

# 6) The REAL live edge — the trailing-exit expectancy the guardrail computes
#    silently every cycle and throws away unless it trips. This surfaces it.
#    Below MIN_TRADES matured, evaluate() returns an "insufficient sample" stub
#    (exactly as it does inside the live bot), so we don't fake a number.
#    Runs outside the cursor block above: evaluate() opens its own connection.
print("\nlive trailing edge (guardrail.evaluate — the number the bot acts on):")
if matured < MIN_TRADES:
    print(f"  -> skipped: only {matured}/{MIN_TRADES} matured rows; "
          f"evaluate() would return 'insufficient sample'.")
else:
    try:
        res = guardrail.evaluate(RUN_TAG, config.PAPER_TRADING)
    except Exception as e:
        print(f"  -> evaluate() error: {e}")
    else:
        if "mean" in res:
            verdict = ("TRIPPED — edge statistically < 0"
                       if res["trip"] else "ok — not statistically dead")
            print(f"  n={res['n']} resolved (last {WINDOW} matured)")
            print(f"  mean  = {res['mean']:+.4f} R   "
                  f"(net of {guardrail.COST_FRAC:.3%} taker round-trip)")
            print(f"  se    = {res['se']:.4f} R")
            print(f"  upper = {res['upper']:+.4f} R   (mean + {guardrail.K_SE}*SE)")
            print(f"  -> guardrail verdict: {verdict}")
            print("  -> NOTE: a positive mean is an OBSERVATION, not a go-live signal. "
                  "This switch can only ever prove the edge dead.")
        else:
            print(f"  -> {res['reason']}")

# 7) WHY the trailing edge can't resolve — per-pair OHLCV store probe.
#    evaluate() recomputes each trade's trailing exit from ENTRY_TF (15m) candles
#    via backtest.store. If store.load() returns <60 bars for every pair, `loaded`
#    is empty and evaluate() reports "insufficient resolved (0/N)" despite having
#    the sample. This replays that exact load step and shows, per pair, what came
#    back — turning an opaque 0/N into a concrete reason. (Hits the exchange once
#    per failing pair, so it may take a moment.)
print("\nstore resolution probe (why evaluate() resolves N rows):")
try:
    import exchange as ex
    from backtest.store import OHLCVStore, now_ts, WARMUP_BARS, TF_SECONDS
    from datetime import datetime as _dt, timezone as _tz

    walk_tf = config.ENTRY_TF
    with perf._cur() as cur:
        cur.execute(
            "SELECT pair, signal_time FROM signal_log "
            "WHERE run_tag=%s AND paper=%s "
            "AND signal_time::timestamp <= NOW() - INTERVAL '72 hours' "
            "ORDER BY signal_time DESC LIMIT %s",
            (RUN_TAG, PAPER, WINDOW),
        )
        probe_rows = [dict(r) for r in cur.fetchall()]

    if len(probe_rows) < MIN_TRADES:
        print(f"  -> only {len(probe_rows)}/{MIN_TRADES} matured rows; evaluate() stops "
              f"at the sample gate before loading OHLCV. Nothing to probe yet.")
    else:
        pairs = sorted({r["pair"] for r in probe_rows})
        tss = [guardrail._sig_ts(r) for r in probe_rows]
        window_start = min(tss) - 86400
        fetch_start = window_start - WARMUP_BARS * TF_SECONDS[walk_tf]
        end = now_ts()
        print(f"  tf={walk_tf}  pairs={len(pairs)}  window: "
              f"{_dt.fromtimestamp(fetch_start, _tz.utc):%Y-%m-%d %H:%M} → "
              f"{_dt.fromtimestamp(end, _tz.utc):%Y-%m-%d %H:%M} UTC")
        print(f"  load() counts a pair only if it returns >=60 bars.\n")

        store = OHLCVStore(0)
        ok = 0
        for p in pairs:
            try:
                loaded = store.load(p, window_start, end, tfs=(walk_tf,))
            except Exception as e:
                print(f"  {p:14} load()=RAISED  {e}")
                continue
            if loaded:
                print(f"  {p:14} load()=OK")
                ok += 1
            else:
                # load() bailed (None or <60 bars) — direct-fetch to show how many it saw.
                try:
                    df = ex.fetch_ohlcv_between(p, walk_tf, fetch_start, end, limit=5000)
                    nbars = 0 if df is None else len(df)
                except Exception as e:
                    nbars = f"fetch error: {e}"
                print(f"  {p:14} load()=FAIL   direct_fetch_bars={nbars}")

        print(f"\n  -> {ok}/{len(pairs)} pairs loaded.")
        if ok == 0:
            print("  -> ALL failed → evaluate() resolves 0. Read direct_fetch_bars:")
            print("       0          → symbol/fetch problem (KuCoin rejects the symbol/range)")
            print("       1-59       → thin 15m history vs the >=60-bar gate")
            print("       60+        → fetch is fine; the bug is downstream of load()")
        else:
            print("  -> some pairs load; evaluate() should resolve rows on the next run.")
except Exception as e:
    print(f"  -> probe error: {e}")
