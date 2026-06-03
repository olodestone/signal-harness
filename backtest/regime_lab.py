"""
regime_lab — does sitting out choppy regimes improve net expectancy?

The 1-year matrix flagged whipsaw months (Oct/Dec/Apr) where everything lost. This tests a
causal regime-strength gate on the 1d frame, applied at signal time (no lookahead), on the
validated config (bt_trend+bt_sweep, 4h-close entry, 1h trailing exit, cost-adjusted):

  ADX(14)            — trend strength; skip when below threshold (choppy)
  Kaufman ER(14)     — |net move| / sum|moves|; skip when below threshold (inefficient/whippy)

Reports net per *kept* trade + kept-fraction per threshold, the per-month effect (does it skip
the bad months?), and per-strategy — IN-SAMPLE and OUT-OF-SAMPLE (the threshold must help both,
else it's curve-fit). Self-contained; writes NOTHING to the DB.

    python -m backtest.regime_lab --offset-days 0     # in-sample
    python -m backtest.regime_lab --offset-days 365   # out-of-sample
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

import config
import exchange as ex
from .store import OHLCVStore, now_ts
from .entry_lab import base_signals, outcome

COST = {"maker": 0.0010, "taker": 0.0030}
ADX_THRESHOLDS = [18, 22, 25]
ER_THRESHOLDS = [0.25, 0.30, 0.40]


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ADX — trend strength (causal)."""
    high, low, close = df["high"], df["low"], df["close"]
    up, dn = high.diff(), -low.diff()
    plus_dm = ((up > dn) & (up > 0)) * up.clip(lower=0)
    minus_dm = ((dn > up) & (dn > 0)) * dn.clip(lower=0)
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def efficiency_ratio(close: pd.Series, n: int = 14) -> pd.Series:
    """Kaufman ER — directional efficiency 0..1 (causal)."""
    change = (close - close.shift(n)).abs()
    vol = close.diff().abs().rolling(n).sum()
    return change / vol.replace(0, np.nan)


def run(days: int, offset_days: int, tags: list) -> None:
    universe = [p for p in ex.get_watchlist() if p not in config.PAIR_BLACKLIST]
    now = now_ts()
    window_end = now - offset_days * 86400
    window_start = window_end - days * 86400
    fetch_end = min(now, window_end + 5 * 86400)
    print(f"[regime_lab] window {datetime.utcfromtimestamp(window_start):%Y-%m-%d} → "
          f"{datetime.utcfromtimestamp(window_end):%Y-%m-%d}"
          + (f"  (OOS {offset_days}d ago)" if offset_days else "  (in-sample)"), flush=True)

    store = OHLCVStore(days)
    loaded = []
    for p in universe:
        if store.load(p, window_start, fetch_end, tfs=("1h", "4h", "1d")):
            d1 = store._data[p]["1d"]["df"]
            d1["adx"] = adx(d1)
            d1["er"] = efficiency_ratio(d1["close"])
            loaded.append(p)
    print(f"[regime_lab] loaded {len(loaded)}/{len(universe)} pairs", flush=True)

    sigs = []
    for p in loaded:
        sigs += base_signals(store, p, window_start, window_end, tags)
    print(f"[regime_lab] {len(sigs)} base setups\n", flush=True)

    recs = []
    for s in sigs:
        d1 = store.as_of(s["pair"], "1d", s["ts"])
        if d1 is None or len(d1) < 20:
            continue
        a = float(d1["adx"].iloc[-1]) if pd.notna(d1["adx"].iloc[-1]) else 0.0
        e = float(d1["er"].iloc[-1]) if pd.notna(d1["er"].iloc[-1]) else 0.0
        res = outcome(store, s["pair"], s["entry"], s["sl"],
                      1 if s["dir"] == "BUY" else -1, s["ts"])
        if res is None:
            continue
        gross, sf = res
        recs.append({"tag": s["tag"], "month": datetime.utcfromtimestamp(s["ts"]).strftime("%Y-%m"),
                     "gross": gross, "sf": sf, "adx": a, "er": e})
    if not recs:
        return
    _report(recs, tags)


def _net(rows, cost):
    if not rows:
        return None
    return sum(r["gross"] - cost / r["sf"] for r in rows) / len(rows)


def _report(recs, tags):
    n = len(recs)
    w = 76
    filters = [("none", lambda r: True)]
    filters += [(f"ADX>={t}", (lambda t: lambda r: r["adx"] >= t)(t)) for t in ADX_THRESHOLDS]
    filters += [(f"ER>={t}", (lambda t: lambda r: r["er"] >= t)(t)) for t in ER_THRESHOLDS]

    print("═" * w)
    print(f"  REGIME FILTER · net per KEPT trade (validated 4h+trailing) · {n} setups")
    print("═" * w)
    print("  " + f"{'filter':<10}{'kept%':>8}{'net/kept T':>13}{'net/kept M':>13}")
    print("  " + "-" * (w - 2))
    best = ("none", -9.9)
    for name, fn in filters:
        kept = [r for r in recs if fn(r)]
        t = _net(kept, COST["taker"]); m = _net(kept, COST["maker"])
        kp = len(kept) / n * 100
        ts = f"{t:>+12.3f}R" if t is not None else f"{'·':>13}"
        ms = f"{m:>+12.3f}R" if m is not None else f"{'·':>13}"
        print(f"  {name:<10}{kp:>7.0f}%{ts}{ms}")
        if t is not None and len(kept) >= 200 and t > best[1]:
            best = (name, t, fn)
    print("  " + "-" * (w - 2))
    print(f"  best (≥200 trades) @taker: {best[0]} ({best[1]:+.3f}R/trade)")
    print()

    # per-month: unfiltered vs best filter (does it skip the bad months?)
    bestfn = next((fn for nm, fn in filters if nm == best[0]), lambda r: True)
    months = sorted({r["month"] for r in recs})
    print(f"  per-month net/kept @taker · none vs {best[0]}:")
    for ym in months:
        allm = [r for r in recs if r["month"] == ym]
        keptm = [r for r in allm if bestfn(r)]
        a = _net(allm, COST["taker"]); b = _net(keptm, COST["taker"])
        astr = f"{a:+.3f}" if a is not None else "  ·  "
        bstr = f"{b:+.3f}({len(keptm)}/{len(allm)})" if b is not None else f"·(0/{len(allm)})"
        print(f"    {ym}   none {astr:>8}   {best[0]} {bstr}")
    print()

    # per-strategy: none vs best filter
    print(f"  per-strategy net/kept @taker · none vs {best[0]}:")
    for tag in tags:
        allt = [r for r in recs if r["tag"] == tag]
        keptt = [r for r in allt if bestfn(r)]
        a = _net(allt, COST["taker"]); b = _net(keptt, COST["taker"])
        astr = f"{a:+.3f}" if a is not None else "·"
        bstr = f"{b:+.3f} (kept {len(keptt)}/{len(allt)})" if b is not None else "·"
        print(f"    {tag:<10} none {astr:>8}   {best[0]} {bstr}")
    print("═" * w)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--offset-days", type=int, default=0)
    ap.add_argument("--strategies", type=str, default="bt_trend,bt_sweep")
    args = ap.parse_args()
    from .entry_lab import DETECTORS
    tags = [t.strip() for t in args.strategies.split(",") if t.strip() in DETECTORS]
    run(args.days, args.offset_days, tags)


if __name__ == "__main__":
    main()
