"""
Analytics workers and report builders for /edge and /diagnose.

Workers (idempotent — skip already-processed rows):
  populate_snapshots()    — price snapshots at 4h/24h after signal generation
  populate_outcomes()     — walk 1h OHLCV to score unfilled signal outcomes
  populate_post_be_tp1()  — walk 15m OHLCV to check BE TP1 re-hit

Report builders:
  build_edge_overview()   — full funnel + direction accuracy
  build_edge_group()      — per-group breakdown (session/regime/dir/conf/atr/pair/type/pairs)
  build_diagnose()        — 4-layer health check with ✓/⚠/✗ and fix hints
"""
from __future__ import annotations
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

import config
import performance as perf
import exchange as ex

# ── N-gates ───────────────────────────────────────────────────────────────────
_N_DIR_TOTAL    = 15   # total dir snapshots before showing accuracy
_N_DIR_SIDE     = 8    # per-direction minimum
_N_SETUP        = 10   # unfilled resolved before setup quality conclusions
_N_ENTRY_HIT    = 8    # entry-hit signals for win-rate conclusions
_N_EXEC         = 10   # closed trades before execution conclusions
_N_BE           = 5    # BE wins before post-BE TP1 conclusions

# ── Icon helpers ──────────────────────────────────────────────────────────────

def _dir_icon(pct: float) -> str:
    if pct >= 55: return "✓"
    if pct >= 45: return "⚠"
    return "✗"


def _wr_icon(wins: int, total: int) -> str:
    if total == 0: return "⚪"
    pct = wins / total * 100
    if pct >= 60: return "🟢"
    if pct >= 45: return "🟡"
    return "🔴"


def _fmt_dir(n: int, ok: int, label: str = "") -> str:
    """Format a direction accuracy figure with icon and N."""
    if n == 0:
        return f"{label}— (no data)"
    pct = ok / n * 100
    icon = _dir_icon(pct)
    prefix = f"{label}" if label else ""
    return f"{prefix}{icon} {pct:.0f}% ({ok}/{n})"


def _fmt_wr(wins: int, losses: int, tstops: int = 0) -> str:
    closed = wins + losses + tstops
    if closed == 0:
        return "—"
    pct = wins / closed * 100
    icon = _wr_icon(wins, closed)
    return f"{icon} {pct:.0f}% ({wins}W/{losses}L)"


# ── Workers ───────────────────────────────────────────────────────────────────

def populate_snapshots(paper: bool = True) -> int:
    """
    Capture 4h and 24h price snapshots for recently-matured signals.
    Uses a ±4h grace window so a 15-min scan cycle never misses the mark.
    Returns count of rows updated.
    """
    updated = 0

    with perf._cur() as cur:
        cur.execute("""
            SELECT id, pair, direction, price_at_gen,
                   snap_4h IS NULL  AS need_4h,
                   snap_24h IS NULL AS need_24h
            FROM signal_log
            WHERE paper = %s
              AND price_at_gen IS NOT NULL
              AND (
                (snap_4h  IS NULL AND signal_time::TIMESTAMP
                    BETWEEN NOW() - INTERVAL '8 hours'  AND NOW() - INTERVAL '4 hours')
                OR
                (snap_24h IS NULL AND signal_time::TIMESTAMP
                    BETWEEN NOW() - INTERVAL '28 hours' AND NOW() - INTERVAL '24 hours')
              )
            LIMIT 60
        """, (int(paper),))
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return 0

    # Fetch prices once per unique pair to minimise API calls
    pair_price: Dict[str, Optional[float]] = {}
    for row in rows:
        if row["pair"] not in pair_price:
            pair_price[row["pair"]] = ex.get_spot_price(row["pair"])
            time.sleep(0.15)

    for row in rows:
        price = pair_price.get(row["pair"])
        if price is None:
            continue
        direction = row["direction"]
        pg = float(row["price_at_gen"])
        sets: list = []

        if row["need_4h"]:
            dir_4h = price > pg if direction == "BUY" else price < pg
            sets += ["snap_4h=%s, dir_4h=%s"]
            vals  = [price, dir_4h]
        else:
            sets, vals = [], []

        if row["need_24h"]:
            dir_24h = price > pg if direction == "BUY" else price < pg
            sets.append("snap_24h=%s, dir_24h=%s")
            vals += [price, dir_24h]

        if sets:
            sql = "UPDATE signal_log SET " + ", ".join(sets) + " WHERE id=%s"
            flat_vals: list = []
            for v in vals:
                if isinstance(v, list):
                    flat_vals.extend(v)
                else:
                    flat_vals.append(v)
            flat_vals.append(row["id"])
            with perf._cur() as cur:
                # Rebuild properly — sets has combined strings, vals are paired
                cur.execute(
                    f"UPDATE signal_log SET snap_4h = COALESCE(snap_4h, %s), "
                    f"dir_4h = COALESCE(dir_4h, %s), "
                    f"snap_24h = COALESCE(snap_24h, %s), "
                    f"dir_24h = COALESCE(dir_24h, %s) WHERE id = %s",
                    (
                        price if row["need_4h"]  else None,
                        (price > pg if direction == "BUY" else price < pg) if row["need_4h"]  else None,
                        price if row["need_24h"] else None,
                        (price > pg if direction == "BUY" else price < pg) if row["need_24h"] else None,
                        row["id"],
                    ),
                )
            updated += 1

    return updated


def populate_outcomes(paper: bool = True, limit: int = 20) -> int:
    """
    Walk 1h OHLCV for matured signals (>72h) to score setup outcomes, and — for
    paper signals — resolve the deterministic paper-execution funnel (fill +
    win/loss + P&L) from the same replay, so /edge & /diagnose populate fill rate,
    win rate, EV and Total P&L without a live order book. Idempotent — only rows
    where setup_outcome IS NULL. Returns count of rows updated.
    """
    with perf._cur() as cur:
        cur.execute("""
            SELECT id, pair, direction, entry, sl, tp, rr, signal_time, price_at_gen
            FROM signal_log
            WHERE paper = %s
              AND stage = 'expired'
              AND setup_outcome IS NULL
              AND signal_time::TIMESTAMP <= NOW() - INTERVAL '72 hours'
            ORDER BY signal_time DESC
            LIMIT %s
        """, (int(paper), limit))
        rows = [dict(r) for r in cur.fetchall()]

    updated = 0
    for row in rows:
        try:
            result = _compute_outcome(row)
        except Exception as e:
            print(f"[analytics] outcome error signal {row['id']}: {e}", flush=True)
            continue
        if result is None:
            continue

        exec_ = _paper_exec_fields(result, float(row.get("rr") or 0.0), paper)
        with perf._cur() as cur:
            if exec_:
                # Paper: also resolve the execution funnel. stage stays 'expired' so
                # the setup-quality counterfactual (Layer 3) is preserved; reports
                # read fills off the `filled` column, win/loss off `outcome`/`pnl_usd`.
                cur.execute("""
                    UPDATE signal_log
                    SET entry_reached=%s, tp1_reached=%s, sl_reached=%s, setup_outcome=%s,
                        filled=%s, outcome=%s, pnl_usd=%s
                    WHERE id=%s
                """, (
                    result["entry_reached"], result["tp1_reached"],
                    result["sl_reached"], result["setup_outcome"],
                    exec_["filled"], exec_["outcome"], exec_["pnl_usd"],
                    row["id"],
                ))
            else:
                cur.execute("""
                    UPDATE signal_log
                    SET entry_reached=%s, tp1_reached=%s, sl_reached=%s, setup_outcome=%s
                    WHERE id=%s
                """, (
                    result["entry_reached"], result["tp1_reached"],
                    result["sl_reached"], result["setup_outcome"],
                    row["id"],
                ))
        updated += 1

    return updated


def _paper_exec_fields(result: Dict, rr: float, paper: bool) -> Dict:
    """Map the 72h replay onto a deterministic paper-execution result.

    Paper only — live fills come from the exchange and are never synthesised here.
    Conventions (conservative, fixed-TP; tune as needed):
      • fill = price touched entry within 72h (entry_reached)
      • win  = TP1 touched first        → +rr R
      • loss = SL touched first         → −1 R
      • TP & SL inside the same 1h bar  → assume SL first (loss)
      • entry hit, neither in 72h       → time stop, flat $0
      • risk/trade = ACCOUNT_CAPITAL × MAX_RISK_PER_TRADE
    P&L is GROSS fixed-TP R (the ~breakeven-by-design /diagnose number); the
    cost-netted *trailing* edge stays in guardrail.evaluate() / check_signal_log.
    Returns {} for live, so only the setup-quality columns get written.
    """
    if int(paper) != 1:
        return {}
    if not result["entry_reached"]:
        return {"filled": 0, "outcome": "unfilled", "pnl_usd": 0.0}
    risk_usd = config.ACCOUNT_CAPITAL * config.MAX_RISK_PER_TRADE
    tp1, sl = result["tp1_reached"], result["sl_reached"]
    if tp1 and not sl:
        outcome, pnl = "tp", risk_usd * rr
    elif tp1 and sl:                        # same-bar TP/SL collision → conservative loss
        outcome, pnl = "sl", -risk_usd
    elif sl:
        outcome, pnl = "sl", -risk_usd
    else:                                   # entry hit, unresolved in 72h → time stop
        outcome, pnl = "time_stop", 0.0
    return {"filled": 1, "outcome": outcome, "pnl_usd": round(pnl, 2)}


def backfill_paper_fills(paper: bool = True) -> int:
    """One-shot, idempotent: populate the paper-execution funnel
    (filled/outcome/pnl_usd) for signals that were already setup-resolved BEFORE
    fill resolution existed. Derives from the stored entry/tp1/sl flags — no candle
    re-walk. Matches only rows with filled IS NULL, so it no-ops once caught up.
    """
    if int(paper) != 1:
        return 0
    with perf._cur() as cur:
        cur.execute("""
            SELECT id, rr, entry_reached, tp1_reached, sl_reached
            FROM signal_log
            WHERE paper = %s AND setup_outcome IS NOT NULL AND filled IS NULL
        """, (int(paper),))
        rows = [dict(r) for r in cur.fetchall()]

    updated = 0
    for row in rows:
        result = {"entry_reached": bool(row["entry_reached"]),
                  "tp1_reached":   bool(row["tp1_reached"]),
                  "sl_reached":    bool(row["sl_reached"])}
        exec_ = _paper_exec_fields(result, float(row.get("rr") or 0.0), paper)
        if not exec_:
            continue
        with perf._cur() as cur:
            cur.execute(
                "UPDATE signal_log SET filled=%s, outcome=%s, pnl_usd=%s WHERE id=%s",
                (exec_["filled"], exec_["outcome"], exec_["pnl_usd"], row["id"]),
            )
        updated += 1
    return updated


def _compute_outcome(row: Dict) -> Optional[Dict]:
    """Walk 1h candles from signal_time to +72h; return entry/tp/sl hit flags."""
    try:
        sig_dt = datetime.fromisoformat(str(row["signal_time"]))
        if sig_dt.tzinfo is None:
            sig_dt = sig_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    start_ts = int(sig_dt.timestamp())
    end_ts   = int((sig_dt + timedelta(hours=72)).timestamp())

    df = ex.fetch_ohlcv_between(row["pair"], "1h", start_ts, end_ts)
    if df is None or len(df) < 2:
        return {"entry_reached": False, "tp1_reached": False,
                "sl_reached": False, "setup_outcome": "no_entry"}

    entry     = float(row["entry"])
    sl        = float(row["sl"])
    tp1       = float(row["tp"])
    direction = row["direction"]

    entry_reached = tp1_reached = sl_reached = False
    phase = "seek_entry"

    for _, bar in df.iterrows():
        hi = float(bar["high"])
        lo = float(bar["low"])

        if phase == "seek_entry":
            if (direction == "BUY"  and lo <= entry) or \
               (direction == "SELL" and hi >= entry):
                entry_reached = True
                phase = "seek_exit"

        elif phase == "seek_exit":
            tp_hit = (direction == "BUY"  and hi >= tp1) or (direction == "SELL" and lo <= tp1)
            sl_hit = (direction == "BUY"  and lo <= sl)  or (direction == "SELL" and hi >= sl)

            if tp_hit and sl_hit:
                tp1_reached = sl_reached = True
                break
            elif tp_hit:
                tp1_reached = True
                break
            elif sl_hit:
                sl_reached = True
                break

    if not entry_reached:
        outcome = "no_entry"
    elif tp1_reached and sl_reached:
        outcome = "ambiguous"
    elif tp1_reached:
        outcome = "win"
    elif sl_reached:
        outcome = "loss"
    else:
        outcome = "ambiguous"   # entry hit, neither TP nor SL reached in 72h

    return {"entry_reached": entry_reached, "tp1_reached": tp1_reached,
            "sl_reached": sl_reached, "setup_outcome": outcome}


def populate_post_be_tp1(paper: bool = True) -> int:
    """
    For BE-win trades (closed at breakeven SL), walk 15m OHLCV to check:
    did price reach TP1 → reverse through entry → reach TP1 again?
    Idempotent — only processes rows where post_be_tp1 IS NULL.
    """
    with perf._cur() as cur:
        cur.execute("""
            SELECT id, pair, direction, entry, tp,
                   fill_time, open_time, close_time
            FROM trades
            WHERE paper = %s
              AND status = 'closed_sl'
              AND post_be_tp1 IS NULL
              AND close_price IS NOT NULL
              AND ABS(close_price - entry) / NULLIF(entry, 0) < 0.005
              AND close_time::TIMESTAMP BETWEEN NOW() - INTERVAL '48 hours'
                                           AND NOW() - INTERVAL '2 hours'
        """, (int(paper),))
        rows = [dict(r) for r in cur.fetchall()]

    updated = 0
    for row in rows:
        try:
            result = _compute_post_be_tp1(row)
        except Exception as e:
            print(f"[analytics] post_be_tp1 error trade {row['id']}: {e}", flush=True)
            result = False

        if result is None:
            continue

        with perf._cur() as cur:
            cur.execute(
                "UPDATE trades SET post_be_tp1=%s WHERE id=%s",
                (result, row["id"]),
            )
        updated += 1

    return updated


def _compute_post_be_tp1(row: Dict) -> Optional[bool]:
    """3-phase candle walk for one BE-win trade."""
    fill_str = row.get("fill_time") or row.get("open_time")
    if not fill_str:
        return None
    try:
        fill_dt = datetime.fromisoformat(str(fill_str))
        if fill_dt.tzinfo is None:
            fill_dt = fill_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    start_ts = int(fill_dt.timestamp())
    end_ts   = int((fill_dt + timedelta(hours=48)).timestamp())

    df = ex.fetch_ohlcv_between(row["pair"], "15m", start_ts, end_ts)
    if df is None or len(df) < 3:
        return None

    entry     = float(row["entry"])
    tp1       = float(row["tp"])
    direction = row["direction"]

    # Phase 0: price reaches TP1
    p0 = None
    for i, (_, bar) in enumerate(df.iterrows()):
        if (direction == "BUY"  and float(bar["high"]) >= tp1) or \
           (direction == "SELL" and float(bar["low"])  <= tp1):
            p0 = i
            break
    if p0 is None:
        return False

    # Phase 1: price crosses back through entry (BE level)
    p1 = None
    for i, (_, bar) in enumerate(df.iloc[p0 + 1:].iterrows(), start=p0 + 1):
        if (direction == "BUY"  and float(bar["low"])  <= entry) or \
           (direction == "SELL" and float(bar["high"]) >= entry):
            p1 = i
            break
    if p1 is None:
        return False

    # Phase 2: price reaches TP1 again
    for _, bar in df.iloc[p1 + 1:].iterrows():
        if (direction == "BUY"  and float(bar["high"]) >= tp1) or \
           (direction == "SELL" and float(bar["low"])  <= tp1):
            return True

    return False


def run_report_workers(paper: bool = True) -> None:
    """Run all analytics workers. Called before /edge and /diagnose output."""
    try:
        n_snap = populate_snapshots(paper)
        if n_snap:
            print(f"[analytics] snapshots updated: {n_snap}", flush=True)
    except Exception as e:
        print(f"[analytics] snapshot worker error: {e}", flush=True)

    try:
        n_bf = backfill_paper_fills(paper)
        if n_bf:
            print(f"[analytics] paper fills backfilled: {n_bf}", flush=True)
    except Exception as e:
        print(f"[analytics] paper-fill backfill error: {e}", flush=True)

    try:
        n_out = populate_outcomes(paper)
        if n_out:
            print(f"[analytics] outcomes computed: {n_out}", flush=True)
    except Exception as e:
        print(f"[analytics] outcome worker error: {e}", flush=True)

    try:
        n_be = populate_post_be_tp1(paper)
        if n_be:
            print(f"[analytics] post_be_tp1 updated: {n_be}", flush=True)
    except Exception as e:
        print(f"[analytics] post_be_tp1 worker error: {e}", flush=True)


# ── Report builders ───────────────────────────────────────────────────────────

def build_edge_overview(paper: bool, days: int = 30, run_tag: Optional[str] = None) -> str:
    funnel = perf.edge_funnel(paper, days, run_tag)
    dirs   = perf.edge_direction_stats(paper, days, run_tag=run_tag)["overall"]
    setup  = perf.edge_setup_quality(paper, days, run_tag)

    total    = int(funnel["total"] or 0)
    filled   = int(funnel["filled"] or 0)
    resolved = int(funnel["resolved"] or 0)
    canc     = int(funnel["cancelled"] or 0)
    pending  = max(total - resolved - canc, 0)   # still inside their 72h window
    no_fill  = max(resolved - filled, 0)         # matured, entry never retraced
    wins    = int(funnel["wins"] or 0)
    losses  = int(funnel["losses"] or 0)
    tstops  = int(funnel["time_stops"] or 0)
    pnl     = float(funnel["total_pnl"] or 0)
    since   = (funnel["first_signal"] or "")[:10]
    mode    = "paper" if paper else "live"
    tag_label = run_tag or "all"

    # Fill rate is judged over MATURED signals (filled / resolved), not raw
    # total — signals still inside their 72h window haven't had a chance to fill.
    f_rate = f"{filled/resolved*100:.0f}%" if resolved else "—"

    # Direction accuracy
    n4  = int(dirs["n_4h"] or 0)
    ok4 = int(dirs["ok_4h"] or 0)
    n4b = int(dirs["n_4h_buy"] or 0); ok4b = int(dirs["ok_4h_buy"] or 0)
    n4s = int(dirs["n_4h_sell"] or 0); ok4s = int(dirs["ok_4h_sell"] or 0)
    n24  = int(dirs["n_24h"] or 0)
    ok24 = int(dirs["ok_24h"] or 0)
    n24b = int(dirs["n_24h_buy"] or 0); ok24b = int(dirs["ok_24h_buy"] or 0)
    n24s = int(dirs["n_24h_sell"] or 0); ok24s = int(dirs["ok_24h_sell"] or 0)

    if n4 >= _N_DIR_TOTAL:
        p4 = ok4 / n4 * 100
        d4_all = f"{_dir_icon(p4)} {p4:.0f}% ({ok4}/{n4})"
        buy4  = f"BUY:{_dir_icon(ok4b/n4b*100) if n4b >= _N_DIR_SIDE else '?'}{ok4b/n4b*100:.0f}%" if n4b else "BUY:—"
        sell4 = f"SELL:{_dir_icon(ok4s/n4s*100) if n4s >= _N_DIR_SIDE else '?'}{ok4s/n4s*100:.0f}%" if n4s else "SELL:—"
    else:
        d4_all = f"⏳ need {_N_DIR_TOTAL - n4} more samples"
        buy4 = sell4 = ""

    if n24 >= _N_DIR_TOTAL:
        p24 = ok24 / n24 * 100
        d24_all = f"{_dir_icon(p24)} {p24:.0f}% ({ok24}/{n24})"
        buy24  = f"BUY:{_dir_icon(ok24b/n24b*100) if n24b >= _N_DIR_SIDE else '?'}{ok24b/n24b*100:.0f}%" if n24b else "BUY:—"
        sell24 = f"SELL:{_dir_icon(ok24s/n24s*100) if n24s >= _N_DIR_SIDE else '?'}{ok24s/n24s*100:.0f}%" if n24s else "SELL:—"
    else:
        d24_all = f"⏳ need {_N_DIR_TOTAL - n24} more samples"
        buy24 = sell24 = ""

    dir_lines = [f"Dir@4h:  {d4_all}"]
    if buy4:
        dir_lines.append(f"         {buy4}  {sell4}")
    dir_lines.append(f"Dir@24h: {d24_all}")
    if buy24:
        dir_lines.append(f"         {buy24}  {sell24}")

    # Execution win rate
    closed = wins + losses + tstops
    wr_line = (
        f"Win rate: {_fmt_wr(wins, losses, tstops)} | EV: ${pnl/filled:+.2f}/trade"
        if filled else "Win rate: — | EV: —"
    )

    # Setup quality
    n_res   = int(setup["n_resolved"] or 0)
    n_hit   = int(setup["n_entry_hit"] or 0)
    n_win_s = int(setup["n_wins"] or 0)
    sq_lines: list = []
    if n_res >= _N_SETUP:
        hit_pct = n_hit / n_res * 100
        sq_lines.append(
            f"Setup quality ({n_res} resolved): "
            f"entry hit {hit_pct:.0f}% | "
            + (f"win/hit {n_win_s/n_hit*100:.0f}%" if n_hit >= _N_ENTRY_HIT else "win/hit ⏳")
        )

    lines = [
        f"📊 *Signal Edge* ({mode} · {days}d · {tag_label})",
        f"Since: {since}" if since else "",
        "",
        f"Generated:  {total}",
        f"├ Filled:   {filled}  ({f_rate} of {resolved} matured)",
        f"├ No-fill:  {no_fill}  (entry never retraced ≤72h)",
        f"├ Pending:  {pending}  (inside 72h window)",
        f"└ Skipped:  {canc}  (gates: dedup/risk/distance)",
        "",
        *dir_lines,
        "",
        wr_line,
        f"Total P&L: ${pnl:+.2f}",
        *sq_lines,
        "",
        "_/edge atr|session|regime|dir|conf|type|pairs | /edge pair SYMBOL_",
    ]
    return "\n".join(l for l in lines if l is not None)


def build_edge_group(paper: bool, group: str, days: int = 30,
                     pair_symbol: Optional[str] = None,
                     run_tag: Optional[str] = None) -> str:
    """
    Build grouped /edge breakdown combining direction accuracy + win rate.
    group: session|regime|dir|conf|type|quality|atr|pairs|pair
    """
    mode = "paper" if paper else "live"
    tag_label = run_tag or "all"

    # ── ATR buckets ──────────────────────────────────────────────────────────
    if group == "atr":
        rows = perf.edge_atr_buckets(paper, days, run_tag)
        if not rows:
            return f"📊 No ATR data yet (need atr field — signals logged after upgrade)."
        lines = [f"📊 *Edge by ATR% Environment* ({mode} · {days}d · {tag_label})\n"]
        for r in rows:
            n4  = int(r["n_4h"] or 0)
            ok4 = int(r["ok_4h"] or 0)
            w   = int(r["wins"] or 0)
            l   = int(r["losses"] or 0)
            pnl = float(r["avg_pnl"] or 0)
            d4  = _fmt_dir(n4, ok4) if n4 >= 5 else f"⏳({n4})"
            ev  = f"EV:${pnl:+.2f}" if (w + l) > 0 else "EV:—"
            lines.append(
                f"*{r['bucket']}* [{int(r['total'])} sig]\n"
                f"  Dir@4h:{d4}  {_fmt_wr(w, l)}  {ev}"
            )
        return "\n".join(lines)

    # ── Pair-specific funnel ─────────────────────────────────────────────────
    if group == "pair" and pair_symbol:
        sym = pair_symbol.upper()
        rows = perf.edge_direction_stats(paper, days, "pair", run_tag=run_tag).get("rows", [])
        row  = next((r for r in rows if (r["group_key"] or "").upper() == sym), None)
        if not row:
            return f"📊 No data for *{sym}* in the last {days}d."

        funnel = perf.edge_funnel(paper, days, run_tag)   # global for context
        n4  = int(row["n_4h"] or 0);  ok4 = int(row["ok_4h"] or 0)
        n24 = int(row["n_24h"] or 0); ok24 = int(row["ok_24h"] or 0)
        w   = int(row["wins"] or 0);  l = int(row["losses"] or 0); ts = int(row["time_stops"] or 0)
        pnl = float(row["avg_pnl"] or 0)
        q   = int(row["went_to_queue"] or 0); f = int(row["filled"] or 0)
        t   = int(row["total"] or 0)

        lines = [
            f"📊 *{sym}* ({mode} · {days}d · {tag_label})",
            "",
            f"Signals: {t}  Queued: {q}  Filled: {f}",
            f"Dir@4h:  {_fmt_dir(n4, ok4)}",
            f"Dir@24h: {_fmt_dir(n24, ok24)}",
            f"Win rate: {_fmt_wr(w, l, ts)}",
            f"Avg EV: ${pnl:+.2f}/trade" if (w + l + ts) else "Avg EV: —",
        ]
        return "\n".join(lines)

    # ── All-pairs ranked table ───────────────────────────────────────────────
    if group == "pairs":
        rows = perf.edge_pairs_ranked(paper, days, run_tag)
        if not rows:
            return "📊 No pair data yet."
        lines = [f"📊 *Pairs Ranked* ({mode} · {days}d · {tag_label})\n"]
        for r in rows:
            pair   = r["pair"] or "?"
            t      = int(r["total"] or 0)
            q      = int(r["went_to_queue"] or 0)
            f      = int(r["filled"] or 0)
            n_dir  = int(r["n_dir"] or 0)
            ok_dir = int(r["ok_dir"] or 0)
            w      = int(r["wins"] or 0)
            l      = int(r["losses"] or 0)
            pnl    = float(r["total_pnl"] or 0)
            n_setup= int(r["n_setup"] or 0)
            n_sw   = int(r["n_setup_win"] or 0)

            dir_pct   = ok_dir / n_dir * 100 if n_dir >= 5 else None
            setup_pct = n_sw / n_setup * 100 if n_setup >= 3 else None
            f_rate    = f"{f/q*100:.0f}%" if q else "—"

            flag = ""
            if dir_pct is not None and dir_pct < 45 and (setup_pct is None or setup_pct < 35):
                flag = " ⚠ BLACKLIST"

            dir_str   = f"{_dir_icon(dir_pct)}{dir_pct:.0f}%" if dir_pct is not None else "—"
            setup_str = f"{setup_pct:.0f}%win" if setup_pct is not None else "—"
            pnl_str   = f"${pnl:+.2f}"

            lines.append(
                f"{pair}{flag}\n"
                f"  fill:{f_rate}  dir:{dir_str}  setup:{setup_str}  P&L:{pnl_str}"
            )
        return "\n".join(lines)

    # ── Grouped breakdown (session/regime/dir/conf/type/quality) ────────────
    group_map = {
        "session":   ("session",          "Session"),
        "regime":    ("regime",           "Regime"),
        "dir":       ("direction",        "Direction"),
        "direction": ("direction",        "Direction"),
        "conf":      ("confidence_band",  "Confidence Band"),
        "type":      ("trade_type",       "Trade Type"),
        "quality":   ("quality",          "Quality"),
        "pair":      ("pair",             "Pair"),
    }
    if group not in group_map:
        return (
            "Usage: /edge atr|session|regime|dir|conf|type|pairs\n"
            "       /edge pair BTC/USDT"
        )

    col, title = group_map[group]
    data  = perf.edge_direction_stats(paper, days, col, run_tag=run_tag)
    rows  = data.get("rows", [])
    if not rows:
        return f"📊 No data yet for *{title}* breakdown."

    _SESSION = {"london_ny": "London/NY", "london": "London",
                "new_york": "New York",  "asian": "Asian"}
    _REGIME  = {"trending_up": "Trend↑", "trending_down": "Trend↓", "ranging": "Ranging"}

    lines = [f"📊 *Edge by {title}* ({mode} · {days}d · {tag_label})\n"]
    for r in rows:
        key   = str(r["group_key"] or "unknown")
        label = _SESSION.get(key, _REGIME.get(key, key))
        t     = int(r["total"] or 0)
        q     = int(r["went_to_queue"] or 0)
        f     = int(r["filled"] or 0)
        n4    = int(r["n_4h"] or 0);  ok4  = int(r["ok_4h"] or 0)
        n24   = int(r["n_24h"] or 0); ok24 = int(r["ok_24h"] or 0)
        w     = int(r["wins"] or 0);  l = int(r["losses"] or 0); ts = int(r["time_stops"] or 0)
        pnl   = float(r["avg_pnl"] or 0)
        rr    = float(r["avg_rr"] or 0)

        d4_str  = _fmt_dir(n4, ok4)  if n4  >= 5 else f"⏳({n4})"
        d24_str = _fmt_dir(n24, ok24) if n24 >= 5 else f"⏳({n24})"
        ev_str  = f"EV:${pnl:+.2f}" if f else "EV:—"

        lines.append(
            f"*{label}* [{t} sig | {f} filled]\n"
            f"  Dir@4h:{d4_str}  Dir@24h:{d24_str}\n"
            f"  {_fmt_wr(w, l, ts)}  {ev_str}  RR:{rr}"
        )

    return "\n".join(lines)


# ── /diagnose builder ─────────────────────────────────────────────────────────

def build_diagnose(paper: bool, run_tag: Optional[str] = None) -> str:
    mode = "paper" if paper else "live"
    tag_label = run_tag or "all"
    issues_critical: List[str] = []
    issues_warn:     List[str] = []
    working:         List[str] = []
    lines = [f"🔬 *Diagnose* ({mode} · {tag_label})\n"]

    funnel = perf.edge_funnel(paper, days=30, run_tag=run_tag)
    dirs   = perf.edge_direction_stats(paper, days=30, run_tag=run_tag)["overall"]
    atrs   = perf.edge_atr_buckets(paper, days=30, run_tag=run_tag)
    setup  = perf.edge_setup_quality(paper, days=30, run_tag=run_tag)
    exec_  = perf.diagnose_execution_stats(paper, run_tag=run_tag)

    total  = int(funnel["total"] or 0)
    filled = int(funnel["filled"] or 0)

    # ── Layer 1: Signal Funnel ────────────────────────────────────────────────
    lines.append("*Layer 1 — Signal Funnel*")
    n_entry_hit = int(setup["n_entry_hit"] or 0)
    n_resolved  = int(setup["n_resolved"] or 0)
    no_entry_n  = int(setup["n_no_entry"] or 0)
    if total == 0:
        lines.append("  ⏳ No signals generated yet")
    elif n_resolved < _N_SETUP:
        # Paper fills resolve via the 72h replay; until a meaningful sample has
        # matured, fill rate is dominated by still-pending signals — not a verdict.
        lines.append(f"  ⏳ Fill rate warming up — {n_resolved}/{_N_SETUP} signals matured "
                     f"past 72h ({filled} paper-filled)")
    else:
        # Judge fill rate over MATURED signals (entry-hit / resolved). filled/queued
        # would be diluted by signals still inside their 72h window, so we avoid it.
        fill_rate     = n_entry_hit / n_resolved * 100
        no_entry_rate = no_entry_n / n_resolved * 100

        fill_icon = "✓" if fill_rate >= 20 else "✗"
        lines.append(f"  Fill rate: {fill_icon} {fill_rate:.0f}% ({n_entry_hit}/{n_resolved} matured)"
                     + ("  ≥20% healthy" if fill_rate >= 20
                        else "  — entries rarely retraced to fill"))

        ne_icon = "✗" if no_entry_rate > 65 else "✓"
        lines.append(f"  No-entry rate: {ne_icon} {no_entry_rate:.0f}% ({no_entry_n}/{n_resolved} resolved)")
        if no_entry_rate > 65:
            issues_critical.append("fill rate: entries rarely retrace to fill")
            lines.append("  → reduce breakout distance or extend the 72h expiry window")
        elif fill_rate < 20:
            issues_critical.append("fill rate <20%")
        else:
            working.append("fill rate")

    # ── Layer 2: Direction Accuracy ───────────────────────────────────────────
    lines.append("\n*Layer 2 — Direction Accuracy*")
    n4  = int(dirs["n_4h"] or 0); ok4 = int(dirs["ok_4h"] or 0)
    n4b = int(dirs["n_4h_buy"] or 0); ok4b = int(dirs["ok_4h_buy"] or 0)
    n4s = int(dirs["n_4h_sell"] or 0); ok4s = int(dirs["ok_4h_sell"] or 0)

    if n4 < _N_DIR_TOTAL:
        lines.append(f"  ⏳ Need {_N_DIR_TOTAL - n4} more snapshots (have {n4}/{_N_DIR_TOTAL})")
    else:
        p4 = ok4 / n4 * 100
        icon4 = _dir_icon(p4)
        lines.append(f"  @4h overall: {icon4} {p4:.0f}% ({ok4}/{n4})")

        for side, n_s, ok_s in [("BUY", n4b, ok4b), ("SELL", n4s, ok4s)]:
            if n_s >= _N_DIR_SIDE:
                ps = ok_s / n_s * 100
                si = _dir_icon(ps)
                lines.append(f"  {side}: {si} {ps:.0f}% ({ok_s}/{n_s})")
                if ps < 45:
                    issues_critical.append(f"{side} direction accuracy {ps:.0f}%")
                    fix = ("tighten long entry filter (require higher confluence)"
                           if side == "BUY"
                           else "tighten short entry filter (counter-trend is harder to call)")
                    lines.append(f"  → {fix}")
                elif ps < 55:
                    issues_warn.append(f"{side} direction borderline ({ps:.0f}%)")
                else:
                    working.append(f"{side} direction")
            else:
                lines.append(f"  {side}: ⏳ need {_N_DIR_SIDE - n_s} more samples")

        if p4 >= 55:
            working.append("overall direction @4h")

    # ── Layer ATR ─────────────────────────────────────────────────────────────
    if atrs:
        lines.append("\n*Layer ATR — Volatility Environment*")
        for r in atrs:
            n4a = int(r["n_4h"] or 0); ok4a = int(r["ok_4h"] or 0)
            if n4a >= 8:
                p = ok4a / n4a * 100
                icon = _dir_icon(p)
                lines.append(f"  {r['bucket']}: {icon} {p:.0f}% dir@4h ({n4a} samples)")
                if p < 45:
                    bucket = r["bucket"]
                    fix = {
                        "<2%":  "avoid quiet markets — insufficient sweep momentum",
                        "2-5%": "core range — investigate signal filter",
                        ">5%":  "too volatile — sweeps are noise, not structure",
                    }.get(bucket, "review entries for this volatility regime")
                    issues_warn.append(f"ATR {bucket} direction {p:.0f}%")
                    lines.append(f"  → {fix}")
            else:
                lines.append(f"  {r['bucket']}: ⏳ ({n4a}/8 samples)")

    # ── Layer 3: Setup Quality ────────────────────────────────────────────────
    lines.append("\n*Layer 3 — Setup Quality (unfilled signals)*")
    n_res  = int(setup["n_resolved"] or 0)
    n_hit  = int(setup["n_entry_hit"] or 0)
    n_wins = int(setup["n_wins"] or 0)

    if n_res < _N_SETUP:
        lines.append(f"  ⏳ Need {_N_SETUP - n_res} more resolved signals (have {n_res})")
    else:
        hit_pct = n_hit / n_res * 100
        hit_icon = "✓" if hit_pct >= 30 else "✗"
        lines.append(f"  Entry hit rate: {hit_icon} {hit_pct:.0f}% ({n_hit}/{n_res})")
        if hit_pct < 30:
            issues_warn.append("entry hit rate <30%")
            lines.append("  → entry level too aggressive — widen retrace tolerance")
        else:
            working.append("entry hit rate")

        if n_hit >= _N_ENTRY_HIT:
            win_pct = n_wins / n_hit * 100
            if win_pct >= 55:
                w_icon = "✓"
                working.append("setup win rate")
            elif win_pct >= 40:
                w_icon = "⚠"
                issues_warn.append(f"setup win rate borderline ({win_pct:.0f}%)")
            else:
                w_icon = "✗"
                issues_critical.append(f"setup win rate {win_pct:.0f}%")
            lines.append(f"  Win when hit:   {w_icon} {win_pct:.0f}% ({n_wins}/{n_hit})")
            if win_pct < 40:
                lines.append("  → SL too tight, or TP1 too ambitious for current volatility")
        else:
            lines.append(f"  Win when hit: ⏳ need {_N_ENTRY_HIT - n_hit} more entry-hit signals")

    # ── Layer 4: Execution ────────────────────────────────────────────────────
    lines.append("\n*Layer 4 — Execution (live closed trades)*")
    n_exec  = int(exec_["total"] or 0)
    n_exw   = int(exec_["wins"] or 0)
    n_exl   = int(exec_["losses"] or 0)
    n_be    = int(exec_["be_wins"] or 0)
    avg_pnl = float(exec_["avg_pnl"] or 0)
    be_chk  = int(exec_["be_tp1_checked"] or 0)
    be_hit  = int(exec_["be_tp1_hit"] or 0)

    if n_exec < _N_EXEC:
        lines.append(f"  ⏳ Need {_N_EXEC - n_exec} more closed trades (have {n_exec})")
    else:
        w_be_total = n_exw + n_be
        wr_pct = w_be_total / n_exec * 100
        wr_icon = "✓" if wr_pct >= 50 else "✗"
        lines.append(f"  Win rate (W+BE): {wr_icon} {wr_pct:.0f}% ({w_be_total}/{n_exec})")
        if wr_pct < 50:
            issues_warn.append(f"execution win rate {wr_pct:.0f}%")

        exp_icon = "✓" if avg_pnl > 0 else "✗"
        lines.append(f"  Expectancy: {exp_icon} ${avg_pnl:+.4f}/trade")
        if avg_pnl <= 0:
            issues_critical.append("negative expectancy")
        else:
            working.append("execution expectancy")

        # Post-BE TP1
        if n_be >= _N_BE and be_chk >= _N_BE:
            be_pct = be_hit / be_chk * 100
            if be_pct >= 60:
                lines.append(
                    f"  Post-BE TP1: ⚠ {be_pct:.0f}% ({be_hit}/{be_chk}) would have hit TP1\n"
                    "  → delay BE activation from 1:1 → 1.3:1 risk"
                )
                issues_warn.append("BE activation too early (60%+ would have hit TP1)")
            elif be_pct >= 50:
                lines.append(
                    f"  Post-BE TP1: ⚠ {be_pct:.0f}% ({be_hit}/{be_chk}) — test 1.2:1 via backtest"
                )
                issues_warn.append("BE activation borderline (50%+ would have hit TP1)")
            else:
                lines.append(f"  Post-BE TP1: ✓ {be_pct:.0f}% ({be_hit}/{be_chk}) — BE timing ok")
                working.append("BE activation timing")
        elif n_be > 0:
            lines.append(f"  Post-BE TP1: ⏳ {be_chk}/{_N_BE} BE wins computed")

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append("\n" + "═" * 28)
    if issues_critical:
        lines.append("🔴 *CRITICAL*: " + " | ".join(issues_critical))
    if issues_warn:
        lines.append("🟡 *WARNINGS*: " + " | ".join(issues_warn))
    if working:
        lines.append("🟢 *WORKING*:  " + " | ".join(working))
    if not issues_critical and not issues_warn:
        lines.append("✅ No issues detected (keep accumulating data)")

    return "\n".join(lines)
