"""
Kill-switch guardrail — one-way safety. Pauses NEW signals when the live trailing edge is
*statistically* below zero over a meaningful sample. It never re-tunes params/pairs/filters
(that overfits, as the regime experiment proved) — it only pauses + alerts.

It measures the REAL edge (the 1h/4h trailing-exit expectancy, net of taker cost) on the
live run_tag — NOT the harness's fixed-TP /diagnose number (which is ~breakeven by design).

Layering: this is the SLOW "the edge is structurally dead" switch. The existing
DAILY_LOSS_LIMIT / LOSS_STREAK_PAUSE machinery are the FAST acute-drawdown switches — they
complement each other.

Resume is manual: review, then restart the bot (a fresh process starts unpaused).
All thresholds are env-tunable.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timezone

import config
import performance as perf
import notify

MIN_TRADES = int(os.getenv("GUARDRAIL_MIN_TRADES", "50"))      # when it STARTS judging (not on noise)
WINDOW = int(os.getenv("GUARDRAIL_WINDOW", "200"))             # rolling window of recent matured trades (~1mo live)
K_SE = float(os.getenv("GUARDRAIL_K_SE", "1.5"))              # confidence: trip when mean + K*SE < 0
# With K_SE=1.5 the trip requires the recent window to be ~1.5 SE below zero — i.e. a large,
# statistically-clear drawdown (order of -15% on the window), NOT a normal bad month. The fast
# acute-drawdown protection is the existing config.DAILY_LOSS_LIMIT / LOSS_STREAK_PAUSE.
COST_FRAC = float(os.getenv("GUARDRAIL_COST_FRAC", "0.003"))   # taker round-trip (conservative)
CHECK_INTERVAL_SEC = int(os.getenv("GUARDRAIL_INTERVAL_SEC", "21600"))   # re-check at most every 6h
STATUS_FILE = os.getenv("GUARDRAIL_STATUS_FILE", "guardrail_status.txt")

_last_check = 0.0


def _sig_ts(row) -> int:
    dt = datetime.fromisoformat(str(row["signal_time"]))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def evaluate(run_tag: str, paper: bool) -> dict:
    """Compute the live trailing-exit net expectancy over the last WINDOW matured trades.
    Returns dict with n, mean, se, upper-confidence-bound, and `trip` (edge statistically < 0)."""
    # lazy imports so a bad backtest import can never stop the bot from running
    from backtest.store import OHLCVStore, now_ts
    from backtest.exit_lab import _trail

    walk_tf = config.ENTRY_TF
    with perf._cur() as cur:
        cur.execute(
            "SELECT pair, direction, entry, sl, signal_time FROM signal_log "
            "WHERE run_tag = %s AND paper = %s "
            "AND signal_time::timestamp <= NOW() - INTERVAL '72 hours' "
            "ORDER BY signal_time DESC LIMIT %s",
            (run_tag, int(paper), WINDOW),
        )
        rows = [dict(r) for r in cur.fetchall()]
    if len(rows) < MIN_TRADES:
        return {"n": len(rows), "trip": False, "reason": f"insufficient sample ({len(rows)}/{MIN_TRADES})"}

    pairs = sorted({r["pair"] for r in rows})
    tss = [_sig_ts(r) for r in rows]
    store = OHLCVStore(0)
    loaded = {p for p in pairs if store.load(p, min(tss) - 86400, now_ts(), tfs=(walk_tf,))}

    nets = []
    for r in rows:
        if r["pair"] not in loaded:
            continue
        sig_ts = _sig_ts(r)
        df = store.between(r["pair"], walk_tf, sig_ts, sig_ts + 72 * 3600, limit=400)
        if df is None or len(df) < 2:
            continue
        entry, sl = float(r["entry"]), float(r["sl"])
        risk = abs(entry - sl)
        if risk <= 0 or entry <= 0:
            continue
        d = 1 if r["direction"] == "BUY" else -1
        H = df["high"].to_numpy(); L = df["low"].to_numpy(); C = df["close"].to_numpy()
        gross = _trail(H, L, C, entry, risk, d, 1.0, 1.0)        # validated: activate 1R, trail 1R
        nets.append(gross - COST_FRAC / (risk / entry))           # net of taker cost in R

    n = len(nets)
    if n < MIN_TRADES:
        return {"n": n, "trip": False, "reason": f"insufficient resolved ({n}/{MIN_TRADES})"}
    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / (n - 1)
    se = math.sqrt(var / n)
    upper = mean + K_SE * se
    return {"n": n, "mean": mean, "se": se, "upper": upper,
            "trip": upper < 0,
            "reason": "edge statistically < 0" if upper < 0 else "edge ok"}


def check_and_maybe_pause(paused: threading.Event,
                          run_tag: str = None, paper: bool = None) -> None:
    """Called once per scan cycle by bot.py. Rate-limited; sets `paused` on a trip."""
    global _last_check
    if paused.is_set():
        return
    now = time.time()
    if now - _last_check < CHECK_INTERVAL_SEC:
        return
    _last_check = now

    run_tag = run_tag or config.RUN_TAG
    paper = config.PAPER_TRADING if paper is None else paper
    try:
        res = evaluate(run_tag, paper)
    except Exception as e:
        print(f"[guardrail] eval error: {e}", flush=True)
        return

    if "mean" in res:
        print(f"[guardrail] n={res['n']} mean={res['mean']:+.4f}R upper={res['upper']:+.4f}R "
              f"trip={res['trip']} ({res['reason']})", flush=True)
    else:
        print(f"[guardrail] {res['reason']}", flush=True)

    if res["trip"]:
        paused.set()
        msg = (f"🛑 GUARDRAIL TRIPPED — new signals PAUSED ({run_tag}).\n"
               f"Live trailing expectancy statistically < 0: mean {res['mean']:+.4f}R, "
               f"upper bound {res['upper']:+.4f}R over n={res['n']} matured trades.\n"
               f"The edge appears broken. Review before resuming (restart the bot to clear).")
        print("[guardrail] " + msg.replace("\n", " | "), flush=True)
        try:
            notify._send(msg)
        except Exception:
            pass
        try:
            with open(STATUS_FILE, "w") as f:
                f.write(datetime.now(timezone.utc).isoformat() + "\n" + msg + "\n")
        except Exception:
            pass
