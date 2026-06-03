"""
Telegram command listener — polls getUpdates in a background daemon thread.
Only accepts commands from TG_CHAT_ID configured in .env.
"""
from __future__ import annotations
import re
import threading
import time
import requests

import config
import performance as perf
import analytics


_BASE = f"https://api.telegram.org/bot{config.TG_TOKEN}" if config.TG_TOKEN else None


def _send(text: str) -> bool:
    if not _BASE or not config.TG_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"{_BASE}/sendMessage",
            json={"chat_id": config.TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        print(f"[commands] Telegram error: {e}")
        return False


def _fmt_sec(sec: int) -> str:
    if sec < 60:   return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m"
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


# ── Command handlers ──────────────────────────────────────────────────────────

def _cmd_report():
    summary = perf.daily_summary()
    _send(perf.format_daily_report(summary))


def _cmd_stats(args: str = ""):
    tag = args.strip().lower() or None
    # "all" means no tag filter; anything else filters to that specific tag
    run_tag_filter = None if tag == "all" else (tag or config.RUN_TAG)
    label = "All Runs" if tag == "all" else f"Run: `{run_tag_filter}`"

    s = perf.all_time_summary(paper=config.PAPER_TRADING, run_tag=run_tag_filter)
    if s["total"] == 0:
        _send(f"📊 *Performance — {label}*\nNo closed trades yet.")
        return

    wr   = s["win_rate"] * 100
    pnl  = s["total_pnl"]
    icon = "🟢" if pnl >= 0 else "🔴"
    cap  = s.get("mfe_capture_pct")
    cap_line  = f"MFE captured: {cap:.1f}%" if cap is not None else "MFE captured: —"
    best      = s["best_trade"]
    worst     = s["worst_trade"]

    _send(
        f"{icon} *Performance — {label}*\n"
        f"Trades: {s['total']} | W:{s['wins']} L:{s['losses']} | WR:{wr:.0f}%\n"
        f"Total P&L: `${pnl:+.2f}` | Avg RR: {s['avg_rr']}\n"
        f"\n"
        f"MAE avg: `${s['avg_mae_usd']:.2f}` ({_fmt_sec(s['avg_mae_time_sec'])} to worst)\n"
        f"MFE avg: `${s['avg_mfe_usd']:.2f}` ({_fmt_sec(s['avg_mfe_time_sec'])} to peak)\n"
        f"{cap_line}\n"
        f"\n"
        f"Best:  {best['pair']} `${best['pnl']:+.2f}`\n"
        f"Worst: {worst['pair']} `${worst['pnl']:+.2f}`\n"
        f"Active since: {s['since']}"
    )


def _cmd_positions():
    open_trades    = perf.get_open_trades(paper=config.PAPER_TRADING)
    pending_trades = perf.get_pending_trades(paper=config.PAPER_TRADING)

    if not open_trades and not pending_trades:
        _send("📭 No open or pending positions.")
        return

    lines = ["*Current Positions*"]

    if open_trades:
        lines.append("\n*Open:*")
        for t in open_trades:
            lines.append(
                f"  {t.direction} {t.pair}\n"
                f"  Entry: `{t.entry}` | SL: `{t.sl}` | TP: `{t.tp}` | RR: 1:{t.rr}"
            )

    if pending_trades:
        lines.append("\n*Pending (awaiting fill):*")
        for t in pending_trades:
            lines.append(
                f"  {t.direction} {t.pair}\n"
                f"  Limit: `{t.entry}` | Conf: {int(t.confidence)}%"
            )

    _send("\n".join(lines))


def _cmd_balance():
    if config.PAPER_TRADING:
        realized, reserved, n_open = perf.paper_state()
        balance = config.PAPER_BALANCE + realized - reserved
        _send(
            f"💼 *Paper Balance*\n"
            f"Available: `${balance:.2f}`\n"
            f"Reserved ({n_open} open trade(s)): `${reserved:.2f}`\n"
            f"All-time P&L: `${realized:+.2f}`\n"
            f"Starting capital: `${config.PAPER_BALANCE:.2f}`"
        )
    else:
        _send("💼 *Live Balance*\nLive execution not yet implemented.")


def _cmd_history(args: str):
    try:
        limit = int(args.strip()) if args.strip() else 10
        limit = max(1, min(limit, 20))
    except ValueError:
        limit = 10

    trades = perf.get_recent_trades(limit=limit, paper=config.PAPER_TRADING)

    if not trades:
        _send("📭 No closed trades yet.")
        return

    lines = [f"*Last {len(trades)} Closed Trades*\n"]
    for t in trades:
        icon       = "✅" if (t.pnl_usd or 0) > 0 else "❌"
        close_date = t.close_time.strftime("%m-%d %H:%M") if t.close_time else "?"
        lines.append(
            f"{icon} {t.pair} {t.direction} | {close_date}\n"
            f"   `{t.entry}` → `{t.close_price}` | P&L: `${(t.pnl_usd or 0):+.2f}` | RR: 1:{t.rr}"
        )

    _send("\n".join(lines))


def _cmd_export(args: str = ""):
    import io
    import csv
    from datetime import datetime as _dt

    tag = args.strip().lower()
    if tag == "all":
        trades = perf.get_all_trades()
        label = "all"
    else:
        trades = perf.get_all_trades(paper=config.PAPER_TRADING)
        label = "paper" if config.PAPER_TRADING else "live"

    if not trades:
        _send("📭 No trades to export.")
        return

    fields = [
        "id", "run_tag", "paper", "pair", "direction", "strategy",
        "confidence", "status",
        "entry", "sl", "tp", "rr", "qty", "risk_usd",
        "open_time", "fill_time", "close_time", "close_price", "pnl_usd",
        "mae_usd", "mfe_usd", "time_to_mae_sec", "time_to_mfe_sec",
        "pending_until",
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        writer.writerow({
            "id":              t.id,
            "run_tag":         t.run_tag,
            "paper":           int(t.paper),
            "pair":            t.pair,
            "direction":       t.direction,
            "strategy":        t.strategy,
            "confidence":      t.confidence,
            "status":          t.status,
            "entry":           t.entry,
            "sl":              t.sl,
            "tp":              t.tp,
            "rr":              t.rr,
            "qty":             t.qty,
            "risk_usd":        t.risk_usd,
            "open_time":       t.open_time,
            "fill_time":       t.fill_time,
            "close_time":      t.close_time,
            "close_price":     t.close_price,
            "pnl_usd":         t.pnl_usd,
            "mae_usd":         t.mae_usd,
            "mfe_usd":         t.mfe_usd,
            "time_to_mae_sec": t.time_to_mae_sec,
            "time_to_mfe_sec": t.time_to_mfe_sec,
            "pending_until":   t.pending_until,
        })

    csv_bytes = buf.getvalue().encode("utf-8")
    filename = f"cb_bot_{label}_{_dt.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

    if not _BASE or not config.TG_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"{_BASE}/sendDocument",
            data={"chat_id": config.TG_CHAT_ID, "caption": f"📊 {len(trades)} trades — {label}"},
            files={"document": (filename, io.BytesIO(csv_bytes), "text/csv")},
            timeout=30,
        )
        if not resp.ok:
            _send(f"⚠️ Export failed: {resp.text[:200]}")
    except Exception as e:
        _send(f"⚠️ Export error: {e}")


_RUN_TAG_RE = re.compile(r"^v\d+$", re.IGNORECASE)


def _extract_run_tag(args: str):
    """Pull an optional run-tag token ('all' or 'vN') out of an /edge|/diagnose
    arg string. Returns (remaining_args, run_tag_filter):
      • no token  → current config.RUN_TAG (default: show the new regime only)
      • 'all'     → None (pool every run)
      • 'vN'      → that explicit tag
    """
    kept: list = []
    tag = config.RUN_TAG
    found = False
    for tok in args.split():
        if not found and (tok.lower() == "all" or _RUN_TAG_RE.match(tok)):
            tag = None if tok.lower() == "all" else tok.lower()
            found = True
        else:
            kept.append(tok)
    return " ".join(kept), tag


def _cmd_edge(args: str = ""):
    """
    /edge                  — full funnel + direction accuracy overview
    /edge atr              — <2% / 2-5% / >5% ATR environment split
    /edge session          — direction accuracy + win rate by session
    /edge regime           — direction accuracy + win rate by regime
    /edge dir              — BUY vs SELL direction accuracy
    /edge conf             — by confidence band (80-100 / 65-79 / 55-64)
    /edge type             — by trade type
    /edge quality          — high vs medium signal quality
    /edge pairs            — all pairs ranked with fill rate + dir accuracy
    /edge pair BTC/USDT    — pair-specific funnel
    """
    args, run_tag = _extract_run_tag(args)
    parts = args.strip().split(None, 1)
    sub   = parts[0].lower() if parts else ""
    arg2  = parts[1].strip() if len(parts) > 1 else ""

    paper = config.PAPER_TRADING

    # ── /edge (overview) ──────────────────────────────────────────────────────
    if sub in ("", "overview"):
        try:
            msg = analytics.build_edge_overview(paper, days=30, run_tag=run_tag)
        except Exception as e:
            msg = f"⚠️ Edge overview error: {e}"
        _send(msg)
        return

    # ── /edge pair SYMBOL (pair-specific) ────────────────────────────────────
    if sub == "pair":
        if not arg2:
            _send("Usage: /edge pair BTC/USDT")
            return
        try:
            msg = analytics.build_edge_group(paper, "pair", days=30, pair_symbol=arg2, run_tag=run_tag)
        except Exception as e:
            msg = f"⚠️ Edge pair error: {e}"
        _send(msg)
        return

    # ── grouped breakdowns ────────────────────────────────────────────────────
    valid_subs = {
        "atr", "session", "regime", "dir", "direction", "conf",
        "type", "quality", "pairs",
    }
    if sub not in valid_subs:
        _send(
            "*CB-Bot /edge commands*\n"
            "/edge — full funnel + direction accuracy\n"
            "/edge atr — by volatility environment\n"
            "/edge session — by trading session\n"
            "/edge regime — by market regime\n"
            "/edge dir — BUY vs SELL\n"
            "/edge conf — by confidence band\n"
            "/edge type — by trade type\n"
            "/edge quality — high vs medium\n"
            "/edge pairs — all pairs ranked\n"
            "/edge pair BTC/USDT — pair-specific\n"
            "\nDefaults to current run (RUN_TAG). Append `all` to pool every run,\n"
            "or `vN` for a specific run — e.g. /edge dir all, /edge v3"
        )
        return

    try:
        msg = analytics.build_edge_group(paper, sub, days=30, run_tag=run_tag)
    except Exception as e:
        msg = f"⚠️ Edge {sub} error: {e}"
    _send(msg)


def _cmd_diagnose(args: str = ""):
    """
    /diagnose [all|vN] — 4-layer automated health check with ✓/⚠/✗ conclusions.
    Defaults to the current run tag (config.RUN_TAG); pass 'all' to pool every run.
    Runs all analytics workers first (snapshots, outcomes, post-BE TP1)
    then formats the diagnostic report.
    """
    _, run_tag = _extract_run_tag(args)
    paper = config.PAPER_TRADING
    _send("🔍 Running diagnostics… (this may take a moment)")
    try:
        analytics.run_report_workers(paper)
        msg = analytics.build_diagnose(paper, run_tag=run_tag)
    except Exception as e:
        msg = f"⚠️ Diagnose error: {e}"
    _send(msg)


def _cmd_help():
    _send(
        "*CB-Bot Commands*\n"
        "\n"
        "/report — Today's trading summary\n"
        "/stats — Performance for current run tag\n"
        "/stats v1 — Performance for a specific tag\n"
        "/stats all — Combined all-time performance\n"
        "/positions — Open & pending trades\n"
        "/balance — Current balance & P&L\n"
        "/history \\[n\\] — Last n closed trades (default 10, max 20)\n"
        "/export — Download all trades as CSV (current mode)\n"
        "/export all — Download all trades across all modes\n"
        "\n"
        "*Signal Analytics*\n"
        "/edge — Full 30d funnel + direction accuracy overview\n"
        "/edge atr — Split by ATR% volatility environment\n"
        "/edge session — Direction accuracy + win rate by session\n"
        "/edge regime — Direction accuracy + win rate by regime\n"
        "/edge dir — BUY vs SELL direction accuracy\n"
        "/edge conf — By confidence band (80-100 / 65-79 / 55-64)\n"
        "/edge type — By trade type\n"
        "/edge quality — high vs medium signal quality\n"
        "/edge pairs — All pairs ranked (fill rate + dir + blacklist flag)\n"
        "/edge pair BTC/USDT — Pair-specific funnel\n"
        "\n"
        "*Health Check*\n"
        "/diagnose — 4-layer automated health check with fix suggestions\n"
        "  Layer 1: Signal funnel (fill rate, no-entry rate)\n"
        "  Layer 2: Direction accuracy @4h/24h with N-gates\n"
        "  Layer ATR: Accuracy by volatility environment\n"
        "  Layer 3: Setup quality (entry hit rate, win when hit)\n"
        "  Layer 4: Execution (win rate, expectancy, BE TP1 timing)\n"
        "\n"
        "/pause — Pause signal scanning\n"
        "/resume — Resume signal scanning\n"
        "/help — This message"
    )


# ── Dispatcher & poll loop ────────────────────────────────────────────────────

def _dispatch(text: str, paused: threading.Event):
    parts = text.strip().split(None, 1)
    cmd   = parts[0].lower().split("@")[0]  # strip @botname suffix
    args  = parts[1] if len(parts) > 1 else ""

    if   cmd == "/report":    _cmd_report()
    elif cmd == "/stats":     _cmd_stats(args)
    elif cmd == "/positions": _cmd_positions()
    elif cmd == "/balance":   _cmd_balance()
    elif cmd == "/history":   _cmd_history(args)
    elif cmd == "/export":    _cmd_export(args)
    elif cmd == "/edge":      _cmd_edge(args)
    elif cmd == "/diagnose":  _cmd_diagnose(args)
    elif cmd == "/pause":
        paused.set()
        _send("⏸ Signal scanning *paused*. Use /resume to restart.")
    elif cmd == "/resume":
        paused.clear()
        _send("▶️ Signal scanning *resumed*.")
    elif cmd == "/help":      _cmd_help()
    else:
        _send(f"Unknown command: `{cmd}`\nType /help for available commands.")


def _poll_loop(paused: threading.Event):
    if not _BASE or not config.TG_CHAT_ID:
        print("[commands] TG_TOKEN or TG_CHAT_ID not set — command listener disabled", flush=True)
        return

    offset = 0
    print("[commands] Telegram command listener started", flush=True)

    while True:
        try:
            resp = requests.get(
                f"{_BASE}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=35,
            )
            if not resp.ok:
                time.sleep(2)
                continue

            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})

                # Security: only accept from configured chat
                if str(msg.get("chat", {}).get("id", "")) != str(config.TG_CHAT_ID):
                    continue

                text = msg.get("text", "")
                if text and text.startswith("/"):
                    try:
                        _dispatch(text, paused)
                    except Exception as e:
                        print(f"[commands] Dispatch error: {e}")
                        _send(f"⚠️ Error: {e}")

        except Exception as e:
            print(f"[commands] Poll error: {e}")
            time.sleep(5)


def start(paused: threading.Event) -> threading.Thread:
    t = threading.Thread(target=_poll_loop, args=(paused,), daemon=True, name="tg-commands")
    t.start()
    return t
