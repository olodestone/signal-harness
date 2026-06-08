"""
Signal Harness — main loop.

Fetches the coin universe, runs YOUR strategy (strategy.generate_signal), logs
every signal to PostgreSQL, and serves /edge + /diagnose over Telegram. There is
no execution: this harness measures signal + direction + setup QUALITY, not P&L.

Per scan (every SCAN_INTERVAL_SEC):
  fetch watchlist  ->  for each pair: fetch 4 timeframes -> generate_signal
  -> dedup -> log_signal(stage='expired')  ->  capture direction snapshots

Why stage='expired'? With no order placement, every signal is "unfilled by
definition", so it flows straight to the 72h price-replay resolver. That makes
/diagnose Layer 2 (direction accuracy) and Layer 3 (setup quality: entry-hit
rate + win-when-hit) populate with zero execution. Layer 1's fill-rate will read
0% and Layer 4 (execution) will say "need closed trades" — both are expected
until you wire your own order placement, which is out of this harness's scope.

Deployment: Railway worker (`python bot.py`). Paper/analytics only — safe to run.
"""
from __future__ import annotations

import signal as _signal
import threading
import time
from datetime import datetime, timezone

import config
import exchange as ex
import performance as perf
import notify
import analytics
import telegram_commands
import guardrail
from strategy import generate_signal

# ── In-memory signal dedup ──────────────────────────────────────────────────
# Avoid re-logging the same pair+direction+entry every 15-min scan while the
# setup persists. Key -> last-logged epoch; entries older than the window expire.
_DEDUP_WINDOW_SEC = 4 * 3600
_dedup: dict[str, float] = {}


def _dedup_ok(run_tag: str, pair: str, direction: str, entry: float) -> bool:
    """True if this signal is new (or its dedup window has elapsed).

    Keyed by run_tag too, so a pair can fire independently in each stack without
    one suppressing the other."""
    key = f"{run_tag}|{pair}|{direction}|{round(entry, 8)}"
    now = time.time()
    # prune
    for k, ts in list(_dedup.items()):
        if now - ts > _DEDUP_WINDOW_SEC:
            _dedup.pop(k, None)
    if key in _dedup:
        return False
    _dedup[key] = now
    return True


def _scan_once() -> int:
    """One full pass over the watchlist. Returns the number of signals logged."""
    ex.clear_cache()
    watchlist = ex.get_watchlist()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"\n[bot] Scan cycle {now} UTC", flush=True)
    print(f"[bot] Scanning {len(watchlist)} pairs …", flush=True)

    logged = 0
    # Fetch each distinct timeframe once per pair, then feed every stack the
    # frames it needs. The 4h swing (4h/1d) and the MTF probe (15m/1h/4h) share
    # 4h, so this fetches the union — 15m/1h/4h/1d — at one cached call apiece.
    tfs_needed = sorted({
        tf for s in config.STACKS
        for tf in (s.entry_tf, s.confirm_tf, s.trend_tf, s.bias_tf)
    })
    for pair in watchlist:
        if pair in config.PAIR_BLACKLIST:
            continue

        frames = {tf: ex.fetch_ohlcv(pair, tf, limit=250) for tf in tfs_needed}
        had_data = False

        for stack in config.STACKS:
            df_entry   = frames.get(stack.entry_tf)
            df_confirm = frames.get(stack.confirm_tf)
            df_trend   = frames.get(stack.trend_tf)
            df_bias    = frames.get(stack.bias_tf)
            if any(x is None or len(x) < 50 for x in (df_entry, df_confirm, df_trend, df_bias)):
                continue
            had_data = True

            try:
                sig = generate_signal(df_entry, df_confirm, df_trend, df_bias)
            except Exception as e:
                print(f"[scan] {pair} [{stack.run_tag}] — strategy error: {e}", flush=True)
                continue
            if sig is None:
                continue
            if not _dedup_ok(stack.run_tag, pair, sig.direction, sig.entry):
                continue

            # Log it. stage='expired' routes it to the 72h setup-quality replay.
            try:
                perf.log_signal(
                    pair=pair, direction=sig.direction,
                    confidence=int(sig.confidence), quality=sig.quality,
                    session=sig.session, regime=sig.regime,
                    rr=sig.rr, entry=sig.entry, sl=sig.sl, tp=sig.tp,
                    paper=config.PAPER_TRADING, placed=False,
                    atr=getattr(sig, "atr", None), price_at_gen=sig.current_price,
                    stage="expired", trade_type=sig.strategy,
                    run_tag=stack.run_tag,
                )
                logged += 1
                print(
                    f"[scan] {pair} [{stack.run_tag}] {sig.direction} | Conf:{sig.confidence}% ({sig.quality})"
                    f" | RR:1:{sig.rr:.1f} | {sig.regime} | {sig.session}"
                    f" | entry:{sig.entry} mkt:{sig.current_price}",
                    flush=True,
                )
                if stack.notify:
                    notify._send(
                        f"🔔 *{sig.direction} {pair}* [{stack.run_tag}] logged — conf {sig.confidence}% "
                        f"({sig.quality}) | {sig.regime} | {sig.session} | RR 1:{sig.rr:.1f}\n"
                        f"entry `{sig.entry}`  sl `{sig.sl}`  tp `{sig.tp}`\n"
                        f"Pull /edge or /diagnose to evaluate signal quality."
                    )
            except Exception as e:
                print(f"[scan] {pair} [{stack.run_tag}] — log error: {e}", flush=True)

        if not had_data:
            ex.mark_no_data(pair)

    print(f"[bot] Scan done — {len(watchlist)} checked | {logged} signal(s) logged", flush=True)

    # Direction snapshots (dir_4h / dir_24h) for matured signals. Best-effort.
    try:
        analytics.populate_snapshots(config.PAPER_TRADING)
    except Exception as e:
        print(f"[bot] snapshot worker error: {e}", flush=True)

    return logged


def _log_config() -> None:
    """Print the RESOLVED config at startup — the only authoritative record of what's
    actually running. Railway env vars, a stray .env (config.py uses load_dotenv
    override=True), and config.py's own defaults can all disagree; this shows the
    winner. '⚠' marks any value that diverges from the OOS-validated config
    (backtest/results_oos.md): 4h/1d/1d/1d, no confidence gate, exclude BTC."""
    c = config
    ok = lambda cond: "✓" if cond else "⚠"
    btc_excluded = "BTC/USDT" in c.PAIR_BLACKLIST
    lines = [
        "[bot] RESOLVED CONFIG (✓/⚠ vs OOS-validated):",
        f"      paper={c.PAPER_TRADING}  scan={c.SCAN_INTERVAL_SEC}s  max_pairs={c.MAX_PAIRS}  "
        f"dynamic={c.DYNAMIC_DISCOVERY}  min_rr={c.MIN_RR}  risk/trade={c.MAX_RISK_PER_TRADE}  "
        f"capital=${c.ACCOUNT_CAPITAL:.0f}",
        f"      MIN_CONFIDENCE={c.MIN_CONFIDENCE} {ok(c.MIN_CONFIDENCE<=55)} (validated: no conf gate)  "
        f"BTC_excluded={btc_excluded} {ok(btc_excluded)} (validated: exclude BTC)",
        f"      stacks={len(c.STACKS)} (each logged under its own run_tag):",
    ]
    for s in c.STACKS:
        validated = (s.entry_tf == "4h" and s.confirm_tf == "1d"
                     and s.trend_tf == "1d" and s.bias_tf == "1d")
        mark = "✓ validated 4h swing" if validated else "⚠ EXPERIMENT (not validated)"
        lines.append(
            f"      • [{s.run_tag}] {s.label}: entry={s.entry_tf} confirm={s.confirm_tf} "
            f"trend={s.trend_tf} bias={s.bias_tf}  notify={s.notify}  {mark}"
        )
    print("\n".join(lines), flush=True)


def main() -> None:
    perf.init_db()

    paused = threading.Event()
    telegram_commands.start(paused)

    stopping = threading.Event()
    _signal.signal(_signal.SIGTERM, lambda *_: stopping.set())
    _signal.signal(_signal.SIGINT,  lambda *_: stopping.set())

    try:
        notify.startup(config.PAPER_TRADING, ex.get_watchlist(), config.ACCOUNT_CAPITAL)
    except Exception:
        pass

    print(f"[bot] Signal Harness up — run_tag={config.RUN_TAG}, "
          f"scan every {config.SCAN_INTERVAL_SEC}s. No execution (analytics only).",
          flush=True)
    _log_config()

    while not stopping.is_set():
        if paused.is_set():
            time.sleep(5)
            continue
        try:
            _scan_once()
        except Exception as e:
            print(f"[bot] Main loop error: {e}", flush=True)
        # Kill-switch: pause new signals if the live trailing edge is statistically dead.
        try:
            guardrail.check_and_maybe_pause(paused)
        except Exception as e:
            print(f"[bot] guardrail error: {e}", flush=True)
        print(f"[bot] Sleeping {config.SCAN_INTERVAL_SEC}s …\n", flush=True)
        stopping.wait(config.SCAN_INTERVAL_SEC)

    print("[bot] Shutting down.", flush=True)


if __name__ == "__main__":
    main()
