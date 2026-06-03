"""
Telegram notification module.

Alerts:
  - Bot startup
  - Trade opened (ICT Sweep-Breaker format matching screenshot)
  - Trade closed (with P&L)
  - Daily summary
  - Risk alerts (loss limit hit, daily target reached)

Set TG_TOKEN and TG_CHAT_ID in .env to enable. Silently no-ops if unset.
"""
import requests
import config

_BASE = f"https://api.telegram.org/bot{config.TG_TOKEN}" if config.TG_TOKEN else None

_STRATEGY_DISPLAY = {
    "ICT_SWEEP_BREAKER": "ICT Sweep-Breaker",
}

_SESSION_DISPLAY = {
    "london_ny":  "london/ny",
    "london":     "london",
    "new_york":   "new york",
    "asian":      "asian",
}

_REGIME_DISPLAY = {
    "trending_up":   "trending",
    "trending_down": "trending",
    "ranging":       "ranging",
}


def _send(text: str, parse_mode: str = "Markdown") -> bool:
    if not _BASE or not config.TG_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"{_BASE}/sendMessage",
            json={"chat_id": config.TG_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        print(f"[notify] Telegram error: {e}")
        return False


def startup(paper: bool, pairs: list, capital: float):
    mode = "📝 PAPER" if paper else "💰 LIVE"
    _send(
        f"*CB-Bot Started* {mode}\n"
        f"Watching `{len(pairs)}` pairs\n"
        f"Capital: `${capital:.0f}` | Risk/trade: `{config.MAX_RISK_PER_TRADE*100:.1f}%`\n"
        f"Confidence gate: `{config.MIN_CONFIDENCE}/100`\n"
        f"Target: `${config.DAILY_TARGET:.0f}/day` | "
        f"Loss limit: `{config.DAILY_LOSS_LIMIT*100:.0f}%/day`"
    )


def signal_pending(
    pair:          str,
    direction:     str,
    entry:         float,
    current_price: float,
    sl:            float,
    tp:            float,
    rr:            float,
    strategy:      str,
    qty:           float,
    risk_usd:      float,
    target_usd:    float,
    confidence:    int,
    quality:       str,
    regime:        str,
    session:       str,
    equity:        float,
    paper:         bool,
):
    mode       = "📝" if paper else "💰"
    base       = pair.split("/")[0]
    side       = "LONG" if direction == "BUY" else "SHORT"
    strat_name = _STRATEGY_DISPLAY.get(strategy, strategy.replace("_", " ").title())
    sess_str   = _SESSION_DISPLAY.get(session, session)
    regime_str = _REGIME_DISPLAY.get(regime, regime)

    _send(
        f"{mode} *{direction} {base} — LIMIT PENDING*\n"
        f"Strategy: {strat_name} | Confidence: {confidence}%\n"
        f"\n"
        f"Limit entry: `{entry}` (mkt: `{current_price}`)\n"
        f"Stop Loss: `{sl}`\n"
        f"Take Profit: `{tp}`\n"
        f"\n"
        f"Size: {qty} ({base})\n"
        f"Risk: ${risk_usd:.2f} | Target: ${target_usd:.2f}\n"
        f"R:R 1:{rr}\n"
        f"\n"
        f"Equity: ${equity:.2f}\n"
        f"\n"
        f"Awaiting retrace to breaker block\n"
        f"(quality={quality}, regime={regime_str}, session={sess_str})"
    )


def entry_filled(
    pair:      str,
    direction: str,
    entry:     float,
    fill_price: float,
    sl:        float,
    tp:        float,
    rr:        float,
    qty:       float,
    risk_usd:  float,
    paper:     bool,
):
    mode = "📝" if paper else "💰"
    base = pair.split("/")[0]
    side = "LONG" if direction == "BUY" else "SHORT"
    _send(
        f"{mode} *{direction} {base} — ENTRY FILLED*\n"
        f"Limit: `{entry}` → filled @ `{fill_price}`\n"
        f"Stop Loss: `{sl}` | Take Profit: `{tp}`\n"
        f"R:R 1:{rr} | Risk: ${risk_usd:.2f} | Size: {qty} {base}"
    )


def signal_expired(pair: str, direction: str, entry: float, paper: bool):
    mode = "📝" if paper else "💰"
    base = pair.split("/")[0]
    side = "LONG" if direction == "BUY" else "SHORT"
    _send(
        f"{mode} *{direction} {base} — PENDING EXPIRED*\n"
        f"Limit: `{entry}` — price never retraced to breaker"
    )


def trade_closed(pair, direction, entry, close_price, pnl, result, paper):
    mode    = "📝" if paper else "💰"
    icon    = "✅ WIN" if result == "win" else "❌ LOSS"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    _send(
        f"{mode} *Trade Closed — {icon}*\n"
        f"Pair: `{pair}` ({direction})\n"
        f"Entry: `{entry}` → Close: `{close_price}`\n"
        f"P&L: *{pnl_str}*"
    )


def daily_report(summary: dict):
    from performance import format_daily_report
    _send(format_daily_report(summary))


def loss_limit_hit(daily_pnl: float):
    _send(
        f"🚨 *Daily Loss Limit Hit*\n"
        f"P&L today: `${daily_pnl:.2f}`\n"
        f"No new trades will open until tomorrow."
    )


def target_reached(daily_pnl: float):
    _send(
        f"🎯 *Daily Target Reached!*\n"
        f"P&L today: `+${daily_pnl:.2f}` / target `${config.DAILY_TARGET:.0f}`\n"
        f"Bot continues monitoring for more setups."
    )


def consecutive_loss_alert(streak: int, paused: bool):
    icon   = "🚨" if paused else "⚠️"
    action = "Trading PAUSED — send /resume to continue." if paused else "Confidence gate raised to 85 until next win."
    _send(
        f"{icon} *{streak} Consecutive Losses*\n"
        f"{action}"
    )


def streak_reset(streak_was: int):
    _send(
        f"✅ *Loss streak cleared* (was {streak_was} consecutive SLs)\n"
        f"Confidence gate back to normal — trading resumed."
    )


def breakeven_trail(pair: str, direction: str, entry: float, paper: bool):
    mode = "📝" if paper else "💰"
    base = pair.split("/")[0]
    _send(
        f"{mode} *{direction} {base} — STOP MOVED TO BREAKEVEN*\n"
        f"Trade at 2× profit — SL locked at entry `{entry}`\n"
        f"Downside eliminated. Riding to TP."
    )
