"""
Replay driver — seeds the harness DB with back-dated signals + resolved outcomes so
the EXISTING /edge + /diagnose analytics work over the last N days.

Flow:
  init DB  ->  freeze universe  ->  prefetch all OHLCV  ->  walk 15m bars (no lookahead)
  ->  per strategy: dedup + INSERT signals dated at the bar's close
  ->  resolve setup outcomes (reuse analytics._compute_outcome, fed from the store)
  ->  resolve 4h/24h direction snapshots (from the store)

Nothing is written to the production strategy.py. Run:

    DATABASE_URL=postgresql://postgres:bt@localhost:5433/bt \
        python -m backtest.run_backtest --days 30
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import config
import exchange as ex
import performance as perf

from .store import OHLCVStore, now_ts
from .strategies import REGISTRY

DEDUP_WINDOW_SEC = 4 * 3600          # one signal per pair+direction per 4h (sim time)


# ── signal insertion (mirrors performance.log_signal, but historical timestamp) ──

_INSERT_SQL = """
    INSERT INTO signal_log
      (signal_time, pair, direction, confidence, quality, session, regime,
       rr, entry, sl, tp, paper, placed, skip_reason,
       atr, price_at_gen, stage, trade_type, run_tag)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def _bulk_insert(rows: list) -> None:
    if not rows:
        return
    with perf._cur() as cur:
        cur.executemany(_INSERT_SQL, rows)


# ── replay ───────────────────────────────────────────────────────────────────

def replay(store: OHLCVStore, loaded: list, tags: list, window_start: int, window_end: int) -> dict:
    inserts: dict = {t: [] for t in tags}
    dedup:   dict = {t: {} for t in tags}
    paper = int(config.PAPER_TRADING)
    ET, CT, TT, BT = config.ENTRY_TF, config.CONFIRM_TF, config.TREND_TF, config.BIAS_TF

    for pi, pair in enumerate(loaded, 1):
        dts = store.decision_times(pair, window_start, window_end, ET)
        for dt in dts:
            de = store.as_of(pair, ET, dt)
            if de is None or len(de) < 30:
                continue
            dc = store.as_of(pair, CT, dt)
            dtr = store.as_of(pair, TT, dt)
            db = store.as_of(pair, BT, dt)
            if dc is None or dtr is None or db is None:
                continue
            if len(dc) < 30 or len(dtr) < 10 or len(db) < 10:
                continue

            bt = datetime.utcfromtimestamp(dt)
            for tag in tags:
                fn, ttype = REGISTRY[tag]
                try:
                    sig = fn(de, dc, dtr, db, bar_time=bt)
                except Exception as e:  # a bad bar shouldn't kill the whole run
                    print(f"  [{tag}] {pair} @ {bt:%Y-%m-%d %H:%M} strategy error: {e}", flush=True)
                    continue
                if sig is None:
                    continue
                key = f"{pair}|{sig.direction}"
                last = dedup[tag].get(key)
                if last is not None and (dt - last) < DEDUP_WINDOW_SEC:
                    continue
                dedup[tag][key] = dt
                inserts[tag].append((
                    bt.isoformat(), pair, sig.direction, int(sig.confidence), sig.quality,
                    sig.session, sig.regime, float(sig.rr), float(sig.entry), float(sig.sl),
                    float(sig.tp), paper, 0, None, float(sig.atr), float(sig.current_price),
                    "expired", ttype, tag,
                ))
        print(f"  prefetched+walked {pi}/{len(loaded)}: {pair}", flush=True)

    counts = {}
    for tag in tags:
        _bulk_insert(inserts[tag])
        counts[tag] = len(inserts[tag])
        print(f"[seed] {tag}: inserted {counts[tag]} signals", flush=True)
    return counts


# ── resolution: setup outcomes + 4h/24h direction snapshots ──────────────────

def _walk_immediate(df, entry: float, sl: float, tp: float, direction: str) -> dict:
    """Market-fill outcome: from the signal bar forward, does TP or SL hit first?

    These strategies enter at market (entry == close at signal time), so unlike the
    live pending-limit resolver (`analytics._compute_outcome`, which waits for price to
    *return* to the entry) the fill is immediate — `entry_reached` is always True and we
    score TP-before-SL directly. Walked on 15m bars (finer than the live 1h walk).
    Populates the same columns build_diagnose / edge_setup_quality read.
    """
    tp1 = sl_hit_flag = False
    for _, bar in df.iterrows():
        hi, lo = float(bar["high"]), float(bar["low"])
        tp_hit = (direction == "BUY" and hi >= tp) or (direction == "SELL" and lo <= tp)
        sl_hit = (direction == "BUY" and lo <= sl) or (direction == "SELL" and hi >= sl)
        if tp_hit and sl_hit:                       # both in one bar → can't disambiguate
            tp1 = sl_hit_flag = True
            break
        if tp_hit:
            tp1 = True
            break
        if sl_hit:
            sl_hit_flag = True
            break

    if tp1 and sl_hit_flag:
        outcome = "ambiguous"
    elif tp1:
        outcome = "win"
    elif sl_hit_flag:
        outcome = "loss"
    else:
        outcome = "ambiguous"                       # neither hit within 72h
    return {"entry_reached": True, "tp1_reached": tp1,
            "sl_reached": sl_hit_flag, "setup_outcome": outcome}


def resolve(store: OHLCVStore, tags: list) -> None:
    cutoff_72h = now_ts() - 72 * 3600
    with perf._cur() as cur:
        cur.execute(
            "SELECT id, pair, direction, entry, sl, tp, signal_time, price_at_gen "
            "FROM signal_log WHERE run_tag = ANY(%s) AND stage = 'expired'",
            (tags,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    snap_updates: list = []     # (snap_4h, dir_4h, snap_24h, dir_24h, id)
    out_updates:  list = []     # (entry_reached, tp1_reached, sl_reached, setup_outcome, id)
    for row in rows:
        sig_dt = datetime.fromisoformat(str(row["signal_time"]))
        if sig_dt.tzinfo is None:
            sig_dt = sig_dt.replace(tzinfo=timezone.utc)
        sig_ts = int(sig_dt.timestamp())
        pg = float(row["price_at_gen"])
        direction = row["direction"]

        # direction snapshots (price 4h / 24h later vs price_at_gen)
        p4 = store.price_at(row["pair"], sig_ts + 4 * 3600, config.ENTRY_TF)
        p24 = store.price_at(row["pair"], sig_ts + 24 * 3600, config.ENTRY_TF)
        if p4 is not None or p24 is not None:
            d4 = None if p4 is None else ((p4 > pg) if direction == "BUY" else (p4 < pg))
            d24 = None if p24 is None else ((p24 > pg) if direction == "BUY" else (p24 < pg))
            snap_updates.append((p4, d4, p24, d24, row["id"]))

        # setup outcome — only once fully matured (>=72h old), exactly like live
        if sig_ts <= cutoff_72h:
            df = store.between(row["pair"], config.ENTRY_TF, sig_ts, sig_ts + 72 * 3600, limit=300)
            if df is not None and len(df):
                r = _walk_immediate(df, float(row["entry"]), float(row["sl"]),
                                    float(row["tp"]), direction)
                out_updates.append((r["entry_reached"], r["tp1_reached"],
                                    r["sl_reached"], r["setup_outcome"], row["id"]))

    # one connection, batched writes (per-row reconnect would crawl at 75k signals)
    con = perf._conn()
    try:
        with con:
            with con.cursor() as cur:
                if snap_updates:
                    cur.executemany(
                        "UPDATE signal_log SET snap_4h=%s, dir_4h=%s, snap_24h=%s, "
                        "dir_24h=%s WHERE id=%s", snap_updates,
                    )
                if out_updates:
                    cur.executemany(
                        "UPDATE signal_log SET entry_reached=%s, tp1_reached=%s, "
                        "sl_reached=%s, setup_outcome=%s WHERE id=%s", out_updates,
                    )
    finally:
        con.close()
    print(f"[resolve] snapshots: {len(snap_updates)} | outcomes: {len(out_updates)}", flush=True)


# ── orchestration ──────────────────────────────────────────────────────────

def run(days: int, pairs: list, tags: list, offset_days: int = 0) -> dict:
    """Replay a `days`-long window ending `offset_days` ago. offset_days=0 → ends now
    (in-sample); offset_days=365 → the prior, unseen year (out-of-sample)."""
    perf.init_db()

    if pairs:
        universe = pairs
    else:
        universe = ex.get_watchlist()
    universe = [p for p in universe if p not in config.PAIR_BLACKLIST]
    # entry/confirm/trend/bias timeframes (env-driven via config) — deduped for fetch
    tf_stack = tuple(dict.fromkeys(
        [config.ENTRY_TF, config.CONFIRM_TF, config.TREND_TF, config.BIAS_TF]))
    print(f"[universe] {len(universe)} pairs: {', '.join(universe)}", flush=True)
    print(f"[strategies] {', '.join(tags)}", flush=True)
    print(f"[timeframes] entry={config.ENTRY_TF} confirm={config.CONFIRM_TF} "
          f"trend={config.TREND_TF} bias={config.BIAS_TF}", flush=True)

    now = now_ts()
    window_end = now - offset_days * 86400
    window_start = window_end - days * 86400
    # fetch a few days PAST the window so forward (72h) outcome + 24h snapshot walks resolve
    fetch_end = min(now, window_end + 4 * 86400)
    print(f"[window] {datetime.utcfromtimestamp(window_start):%Y-%m-%d} → "
          f"{datetime.utcfromtimestamp(window_end):%Y-%m-%d}"
          + (f"  (out-of-sample, {offset_days}d ago)" if offset_days else "  (in-sample)"),
          flush=True)
    store = OHLCVStore(days)

    loaded, skipped = [], []
    for pair in universe:
        ok = store.load(pair, window_start, fetch_end, tfs=tf_stack)
        (loaded if ok else skipped).append(pair)
    print(f"[prefetch] loaded {len(loaded)} | skipped (thin data) {len(skipped)}"
          + (f": {', '.join(skipped)}" if skipped else ""), flush=True)

    counts = replay(store, loaded, tags, window_start, window_end)
    resolve(store, tags)
    return {"loaded": loaded, "skipped": skipped, "counts": counts, "days": days}


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the harness DB via historical replay.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--offset-days", type=int, default=0,
                    help="window ends this many days ago (e.g. 365 = prior year, out-of-sample)")
    ap.add_argument("--pairs", type=str, default="", help="comma list; default = live watchlist")
    ap.add_argument("--strategies", type=str, default=",".join(REGISTRY),
                    help="comma list of run_tags; default = all four")
    args = ap.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    tags = [t.strip() for t in args.strategies.split(",") if t.strip() in REGISTRY]
    if not tags:
        print("No valid strategies selected.", file=sys.stderr)
        sys.exit(1)

    run(args.days, pairs, tags, offset_days=args.offset_days)
    print("\n[done] seeding complete — run:  python -m backtest.report", flush=True)


if __name__ == "__main__":
    main()
