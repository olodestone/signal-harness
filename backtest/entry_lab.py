"""
entry_lab — does a REFINED entry beat the plain 4h-close entry, net of costs?

Same 4h setups (bt_trend + bt_sweep), same structural stop, same 1h trailing-exit walk —
only the ENTRY differs:
  baseline : enter at the 4h-bar close                     (the validated method)
  optA     : limit at a 50% retrace toward the stop        (better price; may not fill)
  optB     : enter on the first confirming 1h reversal      (sharper timing; may not confirm)
             candle within EXPIRY_H

Fair metric = NET R per *opportunity* (filled trades contribute their net R, unfilled
contribute 0) — so A/B pay for the trades they miss. Cost model identical to cost_lab:
cost_in_R = cost_frac / stop_frac, and stop_frac differs per variant (A is tighter).

Self-contained: replays in memory, writes NOTHING to the DB (won't disturb the live bot).

    python -m backtest.entry_lab --offset-days 0     # in-sample year
    python -m backtest.entry_lab --offset-days 365   # out-of-sample (prior year)
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime

import config
import exchange as ex
from .store import OHLCVStore, now_ts
from .strategies import gen_trend, gen_sweep
from .indicators import bullish_reversal, bearish_reversal
from .exit_lab import _trail

SIGNAL_TF, CTX_TF, FINE_TF = "4h", "1d", "1h"
FWD_HOURS = 72
EXPIRY_H = 8                 # wait up to 8h (two 4h bars) for a fill / confirmation
RETRACE_F = 0.5              # optA limit = 50% from the 4h close back toward the stop
COST = {"maker": 0.0010, "taker": 0.0030}
DEDUP_SEC = 4 * 3600
DETECTORS = {"bt_trend": gen_trend, "bt_sweep": gen_sweep}
VARIANTS = ["baseline", "optA", "optB"]


def _trail_R(H, L, C, entry, risk, d):
    return _trail(H, L, C, entry, risk, d, 1.0, 1.0)   # activate 1R, trail 1R (validated)


def base_signals(store, pair, ws, we, tags):
    out, dedup = [], {}
    for dt in store.decision_times(pair, ws, we, SIGNAL_TF):
        de = store.as_of(pair, SIGNAL_TF, dt)
        dctx = store.as_of(pair, CTX_TF, dt)
        if de is None or len(de) < 30 or dctx is None or len(dctx) < 30:
            continue
        bt = datetime.utcfromtimestamp(dt)
        for tag in tags:
            try:
                sig = DETECTORS[tag](de, dctx, dctx, dctx, bar_time=bt)
            except Exception:
                sig = None
            if not sig:
                continue
            key = f"{pair}|{sig.direction}"
            if key in dedup and dt - dedup[key] < DEDUP_SEC:
                continue
            dedup[key] = dt
            out.append({"pair": pair, "ts": dt, "dir": sig.direction,
                        "entry": float(sig.entry), "sl": float(sig.sl), "tag": tag})
    return out


def refine_entry(store, s):
    """variant -> (entry_price, fill_ts) or None (no fill)."""
    pair, dt = s["pair"], s["ts"]
    d = 1 if s["dir"] == "BUY" else -1
    entry, sl = s["entry"], s["sl"]
    risk = abs(entry - sl)
    res = {"baseline": (entry, dt), "optA": None, "optB": None}

    fw = store.between(pair, FINE_TF, dt, dt + EXPIRY_H * 3600, limit=400)
    if fw is None or len(fw) < 2:
        return res
    H = fw["high"].to_numpy(); L = fw["low"].to_numpy(); C = fw["close"].to_numpy()
    T = (fw["time"].astype("int64") // 10**9).to_numpy()

    # Option A — limit at a retrace toward the stop
    limit = entry - d * RETRACE_F * risk
    for i in range(len(fw)):
        if (d == 1 and L[i] <= limit) or (d == -1 and H[i] >= limit):
            res["optA"] = (limit, int(T[i])); break

    # Option B — first confirming 1h reversal candle
    for i in range(1, len(fw)):
        sub = fw.iloc[i - 1:i + 1]
        ok = bullish_reversal(sub) if d == 1 else bearish_reversal(sub)
        if ok:
            res["optB"] = (float(C[i]), int(T[i])); break
    return res


def outcome(store, pair, fill_entry, sl, d, fill_ts):
    """(gross_R, stop_frac) under the validated trailing exit, walked on 1h."""
    risk = abs(fill_entry - sl)
    if risk <= 0 or fill_entry <= 0:
        return None
    fw = store.between(pair, FINE_TF, fill_ts, fill_ts + FWD_HOURS * 3600, limit=400)
    if fw is None or len(fw) < 2:
        return None
    H = fw["high"].to_numpy(); L = fw["low"].to_numpy(); C = fw["close"].to_numpy()
    return _trail_R(H, L, C, fill_entry, risk, d), risk / fill_entry


def run(days: int, offset_days: int, tags: list) -> None:
    universe = [p for p in ex.get_watchlist() if p not in config.PAIR_BLACKLIST]
    now = now_ts()
    window_end = now - offset_days * 86400
    window_start = window_end - days * 86400
    fetch_end = min(now, window_end + 5 * 86400)     # buffer for fill + 72h forward walks
    print(f"[entry_lab] window {datetime.utcfromtimestamp(window_start):%Y-%m-%d} → "
          f"{datetime.utcfromtimestamp(window_end):%Y-%m-%d}"
          + (f"  (OOS {offset_days}d ago)" if offset_days else "  (in-sample)"), flush=True)
    print(f"[entry_lab] EXPIRY={EXPIRY_H}h  retrace={RETRACE_F}  exit=trail a1.0 t1.0", flush=True)

    store = OHLCVStore(days)
    loaded = [p for p in universe if store.load(p, window_start, fetch_end, tfs=(FINE_TF, SIGNAL_TF, CTX_TF))]
    print(f"[entry_lab] loaded {len(loaded)}/{len(universe)} pairs", flush=True)

    sigs = []
    for p in loaded:
        sigs += base_signals(store, p, window_start, window_end, tags)
    print(f"[entry_lab] {len(sigs)} base 4h setups\n", flush=True)
    if not sigs:
        return

    # agg[tag_or_ALL][variant] = list of (gross_R, stop_frac); opp[tag] = #setups
    agg = defaultdict(lambda: defaultdict(list))
    opp = defaultdict(int)
    for s in sigs:
        opp[s["tag"]] += 1; opp["ALL"] += 1
        refs = refine_entry(store, s)
        for v in VARIANTS:
            r = refs.get(v)
            if r is None:
                continue
            fill_entry, fill_ts = r
            res = outcome(store, s["pair"], fill_entry, s["sl"],
                          1 if s["dir"] == "BUY" else -1, fill_ts)
            if res is None:
                continue
            agg[s["tag"]][v].append(res); agg["ALL"][v].append(res)

    _report(agg, opp, tags)


def _stats(rows, n_opp, cost):
    if not rows or n_opp == 0:
        return None
    nets = [g - cost / sf for g, sf in rows]
    return {
        "fill": len(rows) / n_opp * 100,
        "gross_filled": sum(g for g, _ in rows) / len(rows),
        "net_filled": sum(nets) / len(nets),
        "net_opp": sum(nets) / n_opp,          # the fair metric (unfilled = 0)
    }


def _report(agg, opp, tags):
    w = 84
    for scope in (["ALL"] + tags if len(tags) > 1 else tags):
        print("═" * w)
        print(f"  ENTRY COMPARISON · {scope} · NET R/trade & R/opportunity (trailing exit)")
        print("═" * w)
        print("  " + f"{'variant':<10}{'fill%':>7}{'gross/fill':>12}"
              f"{'net/fill T':>12}{'net/OPP T':>11}{'net/fill M':>12}{'net/OPP M':>11}")
        print("  " + "-" * (w - 2))
        best_t = (None, -9.9)
        for v in VARIANTS:
            t = _stats(agg[scope][v], opp[scope], COST["taker"])
            m = _stats(agg[scope][v], opp[scope], COST["maker"])
            if t is None:
                print(f"  {v:<10}{'·':>7}"); continue
            print(f"  {v:<10}{t['fill']:>6.0f}%{t['gross_filled']:>+11.3f}R"
                  f"{t['net_filled']:>+11.3f}R{t['net_opp']:>+10.3f}R"
                  f"{m['net_filled']:>+11.3f}R{m['net_opp']:>+10.3f}R")
            if t["net_opp"] > best_t[1]:
                best_t = (v, t["net_opp"])
        print("  " + "-" * (w - 2))
        print(f"  best net/opportunity @taker: {best_t[0]} ({best_t[1]:+.3f}R)")
        print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--offset-days", type=int, default=0)
    ap.add_argument("--strategies", type=str, default="bt_trend,bt_sweep")
    args = ap.parse_args()
    tags = [t.strip() for t in args.strategies.split(",") if t.strip() in DETECTORS]
    run(args.days, args.offset_days, tags)


if __name__ == "__main__":
    main()
