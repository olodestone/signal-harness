"""
Cost-aware exit lab — the decisive test: does the trailing-exit edge survive fees?

Synthesises all three next-step recommendations in one pass over the seeded signals:
  1. COST MODEL    — net expectancy = gross R − cost_in_R, under maker/taker scenarios.
  2. SELECTIVITY   — net sliced by stop-distance, confidence, pair (find any net-positive subset).
  3. TRAIL SWEEP   — several trailing activation/distance settings.

Economics: size so 1R = stop distance = fixed % of capital. Fees are on notional, so in R
units  cost_in_R = cost_frac / stop_frac,  where stop_frac = |entry−sl| / entry. Tight stops
therefore bleed far more to costs. cost_frac is round-trip (fees + slippage + spread).

    DATABASE_URL=... python -m backtest.cost_lab
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

import config
import performance as perf
from .store import OHLCVStore, now_ts
from .strategies import REGISTRY
from .exit_lab import _fixed, _trail

FWD_HOURS = 72
WALK_TF = config.ENTRY_TF

# round-trip cost as a fraction of notional (fees + slippage + spread)
COST_SCENARIOS = {"zero": 0.0, "maker": 0.0010, "taker": 0.0030}

POLICIES = [
    ("fix_2.0",          lambda H, L, C, e, r, d: _fixed(H, L, C, e, r, d, 2.0)),
    ("fix_3.0",          lambda H, L, C, e, r, d: _fixed(H, L, C, e, r, d, 3.0)),
    ("trail a1.0 t1.0",  lambda H, L, C, e, r, d: _trail(H, L, C, e, r, d, 1.0, 1.0)),
    ("trail a1.0 t1.5",  lambda H, L, C, e, r, d: _trail(H, L, C, e, r, d, 1.5, 1.0)),
    ("trail a0.5 t0.75", lambda H, L, C, e, r, d: _trail(H, L, C, e, r, d, 0.75, 0.5)),
    ("trail a1.5 t1.0",  lambda H, L, C, e, r, d: _trail(H, L, C, e, r, d, 1.0, 1.5)),
]
BEST_POLICY = "trail a1.0 t1.0"   # winner from exit_lab; used for the selectivity slices


def _stop_bucket(sf: float) -> str:
    if sf < 0.005:  return "<0.5%"
    if sf < 0.010:  return "0.5-1%"
    if sf < 0.020:  return "1-2%"
    return ">2%"


def run(tags: list) -> None:
    with perf._cur() as cur:
        cur.execute(
            "SELECT pair, direction, entry, sl, confidence, regime, signal_time, run_tag "
            "FROM signal_log WHERE run_tag = ANY(%s) "
            "AND signal_time::timestamp <= NOW() - INTERVAL '%s hours'",
            (tags, FWD_HOURS),
        )
        sigs = [dict(r) for r in cur.fetchall()]
    print(f"[cost_lab] {len(sigs)} matured signals", flush=True)

    pairs = sorted({s["pair"] for s in sigs})
    ts_all = [_ts(s) for s in sigs]
    store = OHLCVStore(0)
    loaded = {p for p in pairs if store.load(p, min(ts_all) - 86400, now_ts(), tfs=(WALK_TF,))}
    print(f"[cost_lab] price data: {len(loaded)}/{len(pairs)} pairs (walk TF {WALK_TF})", flush=True)

    # per-signal record: gross R per policy + meta
    recs = []
    for s in sigs:
        if s["pair"] not in loaded:
            continue
        sig_ts = _ts(s)
        df = store.between(s["pair"], WALK_TF, sig_ts, sig_ts + FWD_HOURS * 3600, limit=300)
        if df is None or len(df) < 4:
            continue
        entry, sl = float(s["entry"]), float(s["sl"])
        if entry <= 0:
            continue
        stop_frac = abs(entry - sl) / entry
        if stop_frac <= 0:
            continue
        risk = abs(entry - sl)
        d = 1 if s["direction"] == "BUY" else -1
        H = df["high"].to_numpy(); L = df["low"].to_numpy(); C = df["close"].to_numpy()
        gross = {name: fn(H, L, C, entry, risk, d) for name, fn in POLICIES}
        recs.append({
            "tag": s["run_tag"], "pair": s["pair"], "conf": int(s["confidence"] or 0),
            "stop_frac": stop_frac, "gross": gross,
        })
    print(f"[cost_lab] evaluated {len(recs)} signals\n", flush=True)
    if not recs:
        return

    _report_net_matrix(tags, recs)
    _report_stop_buckets(tags, recs)
    _report_selectivity(tags, recs)


def _ts(s) -> int:
    return int(datetime.fromisoformat(str(s["signal_time"])).replace(tzinfo=timezone.utc).timestamp())


def _net(rec, policy, cost_frac):
    return rec["gross"][policy] - cost_frac / rec["stop_frac"]


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _fmt(v):
    return f"{v:>+9.3f}R" if v is not None else f"{'·':>10}"


def _report_net_matrix(tags, recs):
    by_tag = defaultdict(list)
    for r in recs:
        by_tag[r["tag"]].append(r)
    for scen, cost in COST_SCENARIOS.items():
        print("═" * 78)
        print(f"  NET EXPECTANCY (R/trade) · cost scenario: {scen} ({cost*100:.2f}% round-trip)")
        print("═" * 78)
        print("  " + f"{'policy':<18}" + "".join(f"{t:>14}" for t in tags))
        print("  " + "-" * 76)
        for name, _ in POLICIES:
            line = f"  {name:<18}"
            for t in tags:
                line += f"{_fmt(_mean([_net(r, name, cost) for r in by_tag[t]])):>14}"
            print(line)
        print()


def _report_stop_buckets(tags, recs):
    print("═" * 78)
    print(f"  NET by STOP-DISTANCE bucket · policy {BEST_POLICY} · TAKER cost (0.30%)")
    print("  (wider stops carry less cost in R — this is the key selectivity lever)")
    print("═" * 78)
    buckets = ["<0.5%", "0.5-1%", "1-2%", ">2%"]
    print("  " + f"{'stop_frac':<10}" + "".join(f"{t:>14}" for t in tags) + f"{'  share':>10}")
    print("  " + "-" * 84)
    cost = COST_SCENARIOS["taker"]
    for b in buckets:
        line = f"  {b:<10}"
        shares = []
        for t in tags:
            rs = [r for r in recs if r["tag"] == t and _stop_bucket(r["stop_frac"]) == b]
            line += f"{_fmt(_mean([_net(r, BEST_POLICY, cost) for r in rs])):>14}"
        # overall share of this bucket across all tags
        alln = len(recs)
        share = sum(1 for r in recs if _stop_bucket(r["stop_frac"]) == b) / alln * 100
        print(line + f"{share:>9.0f}%")
    print()


def _report_selectivity(tags, recs):
    cost = COST_SCENARIOS["taker"]
    print("═" * 78)
    print(f"  SELECTIVITY · policy {BEST_POLICY} · TAKER cost · seeking a net-positive subset")
    print("═" * 78)

    # by confidence band
    print("  by confidence band:")
    bands = [("70-79", 70, 80), ("80-89", 80, 90), ("90+", 90, 200)]
    for label, lo, hi in bands:
        line = f"    {label:<8}"
        for t in tags:
            rs = [r for r in recs if r["tag"] == t and lo <= r["conf"] < hi]
            line += f"{_fmt(_mean([_net(r, BEST_POLICY, cost) for r in rs])):>14}  (n={len(rs)})"[:20]
        print(line)

    # combined filter: wide stop (>=1%) AND high conf (>=80), pooled across strategies
    print("\n  combined filters (pooled, all strategies, " + BEST_POLICY + ", taker):")
    def pool(pred, label):
        rs = [r for r in recs if pred(r)]
        ev = _mean([_net(r, BEST_POLICY, cost) for r in rs])
        gv = _mean([r["gross"][BEST_POLICY] for r in rs])
        print(f"    {label:<34} net={_fmt(ev)}  gross={_fmt(gv)}  n={len(rs)}")
    pool(lambda r: True,                                   "all signals")
    pool(lambda r: r["stop_frac"] >= 0.01,                 "stop >= 1%")
    pool(lambda r: r["stop_frac"] >= 0.02,                 "stop >= 2%")
    pool(lambda r: r["conf"] >= 80,                        "conf >= 80")
    pool(lambda r: r["stop_frac"] >= 0.01 and r["conf"] >= 80, "stop >= 1% AND conf >= 80")
    pool(lambda r: r["stop_frac"] >= 0.02 and r["conf"] >= 80, "stop >= 2% AND conf >= 80")

    # best single pairs (by net, min 100 trades), pooled per pair across the best strategy mix
    print("\n  top pairs by net (>=100 trades, all strategies pooled):")
    bypair = defaultdict(list)
    for r in recs:
        bypair[r["pair"]].append(_net(r, BEST_POLICY, cost))
    ranked = sorted(((p, _mean(v), len(v)) for p, v in bypair.items() if len(v) >= 100),
                    key=lambda x: x[1], reverse=True)
    for p, ev, n in ranked[:6]:
        print(f"    {p:<12} net={ev:+.3f}R  (n={n})")
    print("  " + "-" * 76)
    print("  worst pairs:")
    for p, ev, n in ranked[-4:]:
        print(f"    {p:<12} net={ev:+.3f}R  (n={n})")
    print("═" * 78)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", type=str, default="bt_trend,bt_sweep,bt_trend2,bt_sweep2")
    args = ap.parse_args()
    tags = [t.strip() for t in args.strategies.split(",") if t.strip() in REGISTRY]
    run(tags)


if __name__ == "__main__":
    main()
