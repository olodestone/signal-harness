import os
from dotenv import load_dotenv

load_dotenv(override=True)   # .env takes precedence over any lingering shell env vars

# ── Exchange ──────────────────────────────────────────────────────────────────
EXCHANGE_ID        = os.getenv("EXCHANGE_ID", "kucoin")      # kucoin | binance | bybit
API_KEY            = os.getenv("API_KEY", "")
API_SECRET         = os.getenv("API_SECRET", "")
API_PASSPHRASE     = os.getenv("API_PASSPHRASE", "")         # KuCoin only

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER_TRADING      = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_BALANCE      = float(os.getenv("PAPER_BALANCE", "500"))  # USD

# ── Capital & Targets ─────────────────────────────────────────────────────────
# How much of your account is allocated to this bot (USD).
# The bot sizes positions so daily P&L expectation ≈ DAILY_TARGET
# when win-rate and RR assumptions hold.
ACCOUNT_CAPITAL    = float(os.getenv("ACCOUNT_CAPITAL", "500"))
DAILY_TARGET       = float(os.getenv("DAILY_TARGET", "10"))    # USD

# ── Risk Management ───────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", "0.01"))   # 1% during validation (half-Kelly on thin sample) — size up to 2% once /diagnose confirms positive expectancy on >=10 closed trades
DAILY_LOSS_LIMIT   = float(os.getenv("DAILY_LOSS_LIMIT",   "0.05"))   # stop if -5 %
MAX_OPEN_TRADES     = int(os.getenv("MAX_OPEN_TRADES",      "3"))
LOSS_STREAK_WARNING = int(os.getenv("LOSS_STREAK_WARNING", "3"))   # consecutive SLs before raising confidence gate to 85
LOSS_STREAK_PAUSE   = int(os.getenv("LOSS_STREAK_PAUSE",   "5"))   # consecutive SLs before pausing all new entries
LOSS_STREAK_DECAY_SEC = int(os.getenv("LOSS_STREAK_DECAY_SEC", "21600"))  # reset a stale streak after 6h with no new loss (stops the gate sticking)
ZOMBIE_HOURS        = float(os.getenv("ZOMBIE_HOURS",       "3.0")) # close at market if stuck N hours with no progress
ZOMBIE_MFE_RATIO    = float(os.getenv("ZOMBIE_MFE_RATIO",   "0.75"))# MFE must be < this × risk to be a zombie
MIN_RR             = float(os.getenv("MIN_RR",              "1.8"))    # min reward:risk
MAX_RR             = float(os.getenv("MAX_RR",              "6.0"))    # sanity cap

# ── Pending Order Settings ───────────────────────────────────────────────────
# After a sweep-breaker signal, the bot places a limit at the breaker level and
# waits for price to retrace. PENDING_EXPIRY_BARS * SCAN_INTERVAL_SEC = TTL.
PENDING_EXPIRY_BARS      = int(os.getenv("PENDING_EXPIRY_BARS",      "20"))  # 20 × 15 min = 5 h (order lifespan)
PENDING_EXPIRY_BARS_HIGH = int(os.getenv("PENDING_EXPIRY_BARS_HIGH", "32"))  # 32 × 15 min = 8 h (conf ≥ 85)
# 0 = disabled (validation phase): a breaker retrace on 15m/1h routinely takes
# 1-3 h, so the old 30-min cap cancelled ~75% of signals before they could fill
# and starved the validation sample. The 30-min rule rested on a 4-trade v2 claim
# that recent data contradicts (fast fills are 1/7 in the last 30d). Non-fills now
# expire via PENDING_EXPIRY_BARS (5 h) only. Re-impose a finite cap once the
# fill-latency report (fill_time_report.py) shows latency actually predicts loss.
FILL_TIMEOUT_SEC         = int(os.getenv("FILL_TIMEOUT_SEC",         "0"))    # cancel if not filled within N sec (0 = off)
ENTRY_FILL_TOLERANCE     = float(os.getenv("ENTRY_FILL_TOLERANCE", "0.001"))  # fill within 0.1 %

# ── Signal Filters ────────────────────────────────────────────────────────────
MIN_CONFLUENCE         = int(os.getenv("MIN_CONFLUENCE", "2"))             # legacy, unused
LATE_ENTRY_PCT         = float(os.getenv("LATE_ENTRY_PCT", "0.004"))      # 0.4 % from entry
MAX_ENTRY_DISTANCE_PCT = float(os.getenv("MAX_ENTRY_DISTANCE_PCT", "0.05"))  # skip if entry >5% from market
SL_COOLDOWN_SEC        = int(os.getenv("SL_COOLDOWN_SEC", "14400"))          # 4h pair cooldown once it kicks in
# A single stop-out is the normal cost of trading and shouldn't bench a clean-structure
# major for hours. Only after this many *consecutive* SLs on the same pair (no TP in
# between) does the cooldown apply. Set to 1 to restore "cooldown on every SL".
SL_COOLDOWN_AFTER_LOSSES = int(os.getenv("SL_COOLDOWN_AFTER_LOSSES", "2"))

# ── Pair Blacklist ────────────────────────────────────────────────────────────
# Pairs permanently excluded from trading regardless of watchlist source.
# Defaults block micro-caps with manipulable sweeps (LAB, LUNC) and BTC, which the
# OOS test found cost-bound / flat at 4h (results_oos.md, results_higher_tf.md).
_DEFAULT_BLACKLIST = "BTC/USDT,LAB/USDT,LUNC/USDT,ASTER/USDT,PENGU/USDT,KCS/USDT,UB/USDT,SHARE/USDT"
PAIR_BLACKLIST     = set(
    p.strip()
    for p in os.getenv("PAIR_BLACKLIST", _DEFAULT_BLACKLIST).split(",")
    if p.strip()
)

# ── Confidence Gate ───────────────────────────────────────────────────────────
# Only trade signals that score >= this on the 1-100 confidence scale.
# 55 = effectively no gate (admits the whole basket): the OOS test found confidence
# is NOT a useful filter (70-79 band == 80+), so don't gate higher during validation.
MIN_CONFIDENCE     = int(os.getenv("MIN_CONFIDENCE", "55"))

# ── Pairs ─────────────────────────────────────────────────────────────────────
# CORE list — 20 established majors that are ALWAYS scanned, regardless of their
# (often thin) KuCoin spot volume. Excludes stablecoins, leveraged tokens, and
# low-cap coins. With DYNAMIC_DISCOVERY on, this core is augmented by liquid,
# established pairs discovered at runtime (see below). Override via WATCHLIST env.
_DEFAULT_WATCHLIST = [
    "BTC/USDT",   # Bitcoin
    "ETH/USDT",   # Ethereum
    "BNB/USDT",   # BNB
    "SOL/USDT",   # Solana
    "XRP/USDT",   # XRP
    "DOGE/USDT",  # Dogecoin
    "ADA/USDT",   # Cardano
    "AVAX/USDT",  # Avalanche
    "LINK/USDT",  # Chainlink
    "DOT/USDT",   # Polkadot
    "LTC/USDT",   # Litecoin
    "BCH/USDT",   # Bitcoin Cash
    "NEAR/USDT",  # NEAR Protocol
    "UNI/USDT",   # Uniswap
    "ATOM/USDT",  # Cosmos
    "APT/USDT",   # Aptos
    "OP/USDT",    # Optimism
    "ARB/USDT",   # Arbitrum
    "SUI/USDT",   # Sui
    "TIA/USDT",   # Celestia
]

_env_watchlist     = [p.strip() for p in os.getenv("WATCHLIST", "").split(",") if p.strip()]
WATCHLIST          = _env_watchlist if _env_watchlist else _DEFAULT_WATCHLIST   # always-scanned core
MAX_PAIRS          = int(os.getenv("MAX_PAIRS", "35"))   # total cap: core + dynamic additions

# Hybrid discovery: when on, the core is augmented with dynamically-discovered
# pairs that (a) clear MIN_VOLUME_USDT 24h volume, (b) have >= MIN_PAIR_AGE_DAYS
# of price history (so fresh pump listings are excluded), and (c) aren't
# blacklisted — filling up to MAX_PAIRS total. Set DYNAMIC_DISCOVERY=false to
# scan the core only.
DYNAMIC_DISCOVERY  = os.getenv("DYNAMIC_DISCOVERY", "true").lower() == "true"
MIN_VOLUME_USDT    = float(os.getenv("MIN_VOLUME_USDT", "10000000"))  # 24h vol floor for a dynamic add
MIN_PAIR_AGE_DAYS  = int(os.getenv("MIN_PAIR_AGE_DAYS", "300"))       # min 1d history for a dynamic add (anti-pump-listing)

# ── Timeframes ────────────────────────────────────────────────────────────────
# Defaults = the OOS-validated stack (backtest/results_oos.md): 4h entry, 1d
# confirm/trend/bias. These code defaults are the SOURCE OF TRUTH: railway.toml
# [variables] are NOT reliably applied by Railway (proven — deleting the dashboard
# RUN_TAG fell back to this default, not the toml value). Override via the Railway
# DASHBOARD only if you must, and confirm against the startup CONFIG banner.
ENTRY_TF           = os.getenv("ENTRY_TF",  "4h")
CONFIRM_TF         = os.getenv("CONFIRM_TF", "1d")
TREND_TF           = os.getenv("TREND_TF",  "1d")
BIAS_TF            = os.getenv("BIAS_TF",   "1d")

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_TOKEN           = os.getenv("TG_TOKEN",   "")
TG_CHAT_ID         = os.getenv("TG_CHAT_ID", "")

# ── Run tag ───────────────────────────────────────────────────────────────────
# Label stored on every trade AND signal — bump on any config change that should
# be analysed separately, so /edge, /diagnose and /stats can isolate the new regime
# from the old. v5 = clean 4h validated slice; v1/v2/v4 were all 15m (dashboard
# ENTRY_TF=15m override, then the 15m code-default once the dashboard was cleared).
# This default is authoritative — bump it HERE, not in the dashboard (a dashboard
# value silently overrides and hides this).
RUN_TAG            = os.getenv("RUN_TAG", "v5")

# ── Strategy Stacks (multi-timeframe) ─────────────────────────────────────────
# The harness runs EACH stack below over every pair on every scan and logs its
# signals under its OWN run_tag, so /edge, /diagnose and /stats keep them in
# separate buckets (pass the tag, e.g. `/edge v6`).
#
# Stack 1 — the OOS-validated 4h swing. Driven by ENTRY_TF/CONFIRM_TF/TREND_TF/
#   BIAS_TF + RUN_TAG above, so it stays byte-for-byte the config you already run.
#   Authoritative; do not disturb.
# Stack 2 — the multi-timeframe (MTF) probe: 4h bias → 1h trigger → 15m entry.
#   It is the SAME strategy functions on a faster TF mapping. This is the 15m
#   stack that produced the v1/v2/v4 "junk" slices, so it is an EXPERIMENT
#   (tag v6), NOT validated — a different run_tag keeps it out of the clean v5
#   slice automatically. Disable with ENABLE_MTF=false. notify defaults OFF so the
#   faster 15m cadence doesn't spam Telegram — review the probe via `/edge v6`.
from collections import namedtuple

Stack = namedtuple("Stack", "run_tag entry_tf confirm_tf trend_tf bias_tf label notify")

ENABLE_MTF = os.getenv("ENABLE_MTF", "true").lower() == "true"

STACKS = [
    Stack(RUN_TAG, ENTRY_TF, CONFIRM_TF, TREND_TF, BIAS_TF, "swing_4h", True),
]
if ENABLE_MTF:
    STACKS.append(Stack(
        run_tag    = os.getenv("MTF_RUN_TAG",    "v6"),
        entry_tf   = os.getenv("MTF_ENTRY_TF",   "15m"),
        confirm_tf = os.getenv("MTF_CONFIRM_TF", "1h"),
        trend_tf   = os.getenv("MTF_TREND_TF",   "4h"),
        bias_tf    = os.getenv("MTF_BIAS_TF",    "4h"),
        label      = "mtf_15m",
        notify     = os.getenv("MTF_NOTIFY", "false").lower() == "true",
    ))

# ── Loop ──────────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", "900"))   # 15 min
