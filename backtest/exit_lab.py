"""
Exit / RR lab — does the EXIT side carry the edge that entry filtering didn't?

The win/loss resolver only answers "did a fixed 2:1 TP hit before SL?". To compare exit
rules we need EXPECTANCY (mean R per trade). This reuses the signals already seeded in the
DB (entry / SL / direction / time) and re-walks each one's forward 15m path under several
exit policies — no re-run of the expensive replay; only a 15m-only store rebuild.

R is in risk units: 1R = |entry - sl|. Within-bar ties resolve stop-first (conservative).
Signals that never resolve in 72h are marked-to-market at the 72h close.

    DATABASE_URL=... python -m backtest.exit_lab --strategies bt_trend,bt_sweep,bt_trend2,bt_sweep2
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone

import config
import performance as perf
from .store import OHLCVStore, now_ts
from .strategies import REGISTRY

FWD_HOURS = 72
WALK_TF = config.ENTRY_TF        # walk forward paths on the entry timeframe


# ── exit-policy simulators: return realized R for one signal's forward path ──────

def _fixed(highs, lows, closes, entry, risk, d, k):
    """Fixed TP at kR, fixed SL at -1R."""
    stop = entry - d * risk
    tgt = entry + d * k * risk
    for h, l in zip(highs, lows):
        if d == 1:
            if l <= stop: return -1.0
            if h >= tgt:  return k
        else:
            if h >= stop: return -1.0
            if l <= tgt:  return k
    return d * (closes[-1] - entry) / risk


def _breakeven(highs, lows, closes, entry, risk, d, k, activate=1.0):
    """Move SL to entry once +activate R is reached, then TP at kR."""
    stop = entry - d * risk
    tgt = entry + d * k * risk
    moved = False
    for h, l in zip(highs, lows):
        if d == 1:
            if l <= stop: return d * (stop - entry) / risk
            if not moved and h >= entry + activate * risk:
                moved, stop = True, entry
            if h >= tgt: return k
        else:
            if h >= stop: return d * (stop - entry) / risk
            if not moved and l <= entry - activate * risk:
                moved, stop = True, entry
            if l <= tgt: return k
    return d * (closes[-1] - entry) / risk


def _trail(highs, lows, closes, entry, risk, d, trail=1.0, activate=1.0):
    """Trail SL `trail`R below the peak, armed once +activate R is reached."""
    stop = entry - d * risk
    peak = entry
    active = False
    for h, l in zip(highs, lows):
        if d == 1:
            if l <= stop: return d * (stop - entry) / risk
            peak = max(peak, h)
            if not active and (peak - entry) / risk >= activate:
                active = True
            if active:
                stop = max(stop, peak - trail * risk)
        else:
            if h >= stop: return d * (stop - entry) / risk
            peak = min(peak, l)
            if not active and (entry - peak) / risk >= activate:
                active = True
            if active:
                stop = min(stop, peak + trail * risk)
    return d * (closes[-1] - entry) / risk


# run order matters only for display
POLICIES = [
    ("fix_1.5",  lambda H, L, C, e, r, d: _fixed(H, L, C, e, r, d, 1.5)),
    ("fix_2.0",  lambda H, L, C, e, r, d: _fixed(H, L, C, e, r, d, 2.0)),   # baseline
    ("fix_2.5",  lambda H, L, C, e, r, d: _fixed(H, L, C, e, r, d, 2.5)),
    ("fix_3.0",  lambda H, L, C, e, r, d: _fixed(H, L, C, e, r, d, 3.0)),
    ("be@1->2",  lambda H, L, C, e, r, d: _breakeven(H, L, C, e, r, d, 2.0)),
    ("be@1->3",  lambda H, L, C, e, r, d: _breakeven(H, L, C, e, r, d, 3.0)),
    ("trail1R",  lambda H, L, C, e, r, d: _trail(H, L, C, e, r, d, 1.0, 1.0)),
]


def run(tags: list) -> None:
    cutoff = now_ts() - FWD_HOURS * 3600
    with perf._cur() as cur:
        cur.execute(
            "SELECT pair, direction, entry, sl, signal_time, run_tag FROM signal_log "
            "WHERE run_tag = ANY(%s) AND signal_time::timestamp <= NOW() - INTERVAL '%s hours'",
            (tags, FWD_HOURS),
        )
        sigs = [dict(r) for r in cur.fetchall()]
    if not sigs:
        print("No matured signals for those run_tags.")
        return
    print(f"[exit_lab] {len(sigs)} matured signals across {len(tags)} strategies", flush=True)

    pairs = sorted({s["pair"] for s in sigs})
    ts_all = [int(datetime.fromisoformat(str(s["signal_time"])).replace(tzinfo=timezone.utc).timestamp())
              for s in sigs]
    ws, we = min(ts_all) - 86400, now_ts()
    store = OHLCVStore(0)
    loaded = set()
    for p in pairs:
        if store.load(p, ws, we, tfs=(WALK_TF,)):
            loaded.add(p)
    print(f"[exit_lab] price data: {len(loaded)}/{len(pairs)} pairs loaded (walk TF {WALK_TF})", flush=True)

    # aggregate: (run_tag, policy) -> list of realized R
    agg: dict = defaultdict(lambda: defaultdict(list))
    n_eval = 0
    for s in sigs:
        if s["pair"] not in loaded:
            continue
        sig_ts = int(datetime.fromisoformat(str(s["signal_time"])).replace(tzinfo=timezone.utc).timestamp())
        df = store.between(s["pair"], WALK_TF, sig_ts, sig_ts + FWD_HOURS * 3600, limit=300)
        if df is None or len(df) < 4:
            continue
        entry, sl = float(s["entry"]), float(s["sl"])
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        d = 1 if s["direction"] == "BUY" else -1
        H = df["high"].to_numpy(); L = df["low"].to_numpy(); C = df["close"].to_numpy()
        for name, fn in POLICIES:
            agg[s["run_tag"]][name].append(fn(H, L, C, entry, risk, d))
        n_eval += 1

    print(f"[exit_lab] evaluated {n_eval} signals\n", flush=True)
    _print_matrix(tags, agg, n_eval)


def _print_matrix(tags: list, agg: dict, n_eval: int) -> None:
    tags = [t for t in tags if agg.get(t)]
    w = 78
    print("═" * w)
    print("  EXIT / RR LAB  ·  expectancy = mean R per trade  (1R = initial risk)")
    print("  positive = edge.  baseline is fix_2.0 (the win/loss resolver's rule)")
    print("═" * w)
    header = "  " + f"{'policy':<10}" + "".join(f"{t:>14}" for t in tags)
    print(header)
    print("  " + "-" * (w - 2))
    # expectancy rows
    best_by_tag = {t: (None, -9.9) for t in tags}
    for name, _ in POLICIES:
        line = f"  {name:<10}"
        for t in tags:
            rs = agg[t].get(name, [])
            if not rs:
                line += f"{'·':>14}"; continue
            ev = sum(rs) / len(rs)
            line += f"{ev:>+13.3f}R"
            if ev > best_by_tag[t][1]:
                best_by_tag[t] = (name, ev)
        print(line)
    print("  " + "-" * (w - 2))
    # win-rate row reference for baseline
    line = f"  {'win% @2R':<10}"
    for t in tags:
        rs = agg[t].get("fix_2.0", [])
        wr = (sum(1 for r in rs if r > 0) / len(rs) * 100) if rs else 0
        line += f"{wr:>13.0f}%"
    print(line)
    line = f"  {'n trades':<10}"
    for t in tags:
        line += f"{len(agg[t].get('fix_2.0', [])):>14}"
    print(line)
    print("  " + "-" * (w - 2))
    for t in tags:
        nm, ev = best_by_tag[t]
        verdict = "POSITIVE edge" if ev > 0.02 else ("~flat" if ev > -0.02 else "negative")
        print(f"  {t:<10} best: {nm} → {ev:+.3f}R/trade  ({verdict})")
    print("═" * w)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", type=str, default="bt_trend,bt_sweep,bt_trend2,bt_sweep2")
    args = ap.parse_args()
    tags = [t.strip() for t in args.strategies.split(",") if t.strip() in REGISTRY]
    run(tags)


if __name__ == "__main__":
    main()
