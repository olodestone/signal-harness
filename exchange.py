"""
Exchange abstraction — fetches OHLCV and price data.

Paper mode uses KuCoin public REST API (no API key needed).
Live mode uses the configured exchange with API credentials.
"""
import os
import re
import time
import requests
import pandas as pd
from typing import Optional, List

import config

_KC_URL = "https://api.kucoin.com/api/v1"

# Minimum gap between consecutive KuCoin requests to avoid 429s
_RATE_DELAY    = 0.25
_last_req_ts: float = 0.0

# Range fetches (the guardrail's 15m burst + the outcome resolver) can come back
# EMPTY under a sliding-window soft-throttle — KuCoin returns code 200000 with no
# data rather than a 429, especially from datacenter IPs when a burst of requests
# (scan + guardrail) lands in a short window. That's the "insufficient resolved
# (0/N)" symptom on Railway (works fine run standalone). Back off + retry on empty
# instead of silently giving up. Tunable via env.
_RANGE_RETRIES     = int(os.getenv("OHLCV_RANGE_RETRIES", "5"))
_RANGE_BACKOFF_CAP = float(os.getenv("OHLCV_RANGE_BACKOFF_CAP", "10"))


def _throttle():
    global _last_req_ts
    gap = time.time() - _last_req_ts
    if gap < _RATE_DELAY:
        time.sleep(_RATE_DELAY - gap)
    _last_req_ts = time.time()


# KuCoin interval strings
_TF_MAP = {"15m": "15min", "1h": "1hour", "4h": "4hour", "1d": "1day"}

# ── OHLCV cache ───────────────────────────────────────────────────────────────
_ohlcv_cache: dict = {}
_ohlcv_ts: dict    = {}
_TF_TTL = {"15m": 60, "1h": 600, "4h": 3600, "1d": 43200}

# ── Watchlist cache (refreshed every 4 h) ────────────────────────────────────
_watchlist_cache: List[str] = []
_watchlist_ts: float        = 0.0
_WATCHLIST_TTL              = 14400   # 4 hours

# Pairs that failed OHLCV history checks — excluded until their TTL expires.
# Maps symbol → expiry timestamp so exclusions survive across watchlist refreshes.
# TTL is 7 days, not 24h: a pair that lacks the price history the strategy needs
# won't grow it overnight, and a 24h TTL caused daily churn (exclude → expire →
# re-import top-by-vol → fail → re-exclude) that wasted API budget and triggered 429s.
_data_poor_pairs: dict = {}
_DATA_POOR_TTL = 604800  # 7 days


def mark_no_data(symbol: str) -> None:
    """Register a pair as lacking sufficient OHLCV history; excluded for 24h."""
    _data_poor_pairs[symbol] = time.time() + _DATA_POOR_TTL

# Listing-age memo for dynamic watchlist additions. pair → (is_established, checked_ts).
# "Established" is permanent (a coin's history only grows); "too new" is re-checked weekly.
_established_cache: dict = {}
_ESTABLISHED_RECHECK_TTL = 604800  # 7 days


def _is_established(symbol: str) -> bool:
    """True if the pair has >= MIN_PAIR_AGE_DAYS of 1d history.

    Proxies "not a fresh pump listing" for dynamic watchlist additions. Costs one
    dated 1d range fetch the first time a pair is seen; results are memoised so
    refreshes are cheap. NOTE: this filters by age only — a liquid, established
    but *manipulated* coin still needs the manual PAIR_BLACKLIST.
    """
    cached = _established_cache.get(symbol)
    now = time.time()
    if cached is not None:
        ok, ts = cached
        if ok or (now - ts) < _ESTABLISHED_RECHECK_TTL:
            return ok

    need_days = config.MIN_PAIR_AGE_DAYS
    start = int(now - (need_days + 5) * 86400)
    df = fetch_ohlcv_between(symbol, "1d", start, int(now), limit=need_days + 50)
    ok = df is not None and len(df) >= need_days
    _established_cache[symbol] = (ok, now)
    return ok

# Stablecoins and wrapped/pegged assets to exclude as the base token
_STABLES = {
    "USDC", "USDD", "TUSD", "BUSD", "DAI", "FDUSD", "GUSD",
    "USDP", "FRAX", "LUSD", "CUSD", "SUSD", "EUSD", "PYUSD",
    "UST", "USDJ", "CEUR", "EURS", "ALUSD", "USDN", "MUSD", "USDX", "USDE",
    "WBTC", "WETH", "STETH", "WSTETH", "CBBTC",
}

# Non-crypto real-world assets (gold, silver, oil, agricultural)
_NON_CRYPTO = {
    "XAUT", "PAXG", "CACHE", "XAU", "XAG", "XAGT",
    "SILVER", "GOLD",
    "USOIL", "UKOIL", "OIL", "BRENT", "WTI",
    "WHEAT", "CORN", "SOYB",
}

# Leveraged / synthetic token patterns (e.g. BTC3L, ETHDOWN, 1000SHIB)
_LEVERAGED = re.compile(r"(\d+L|\d+S|UP|DOWN|BULL|BEAR|LONG|SHORT)-USDT$", re.IGNORECASE)
_NUMERIC_PREFIX = re.compile(r"^\d+")


def _to_kc(symbol: str) -> str:
    """BTC/USDT → BTC-USDT"""
    return symbol.replace("/", "-")


def _is_tradeable(symbol: str) -> bool:
    """Return True if the KuCoin symbol is a plain crypto/USDT pair."""
    if not symbol.endswith("-USDT"):
        return False
    base = symbol[:-5]
    if base in _STABLES:
        return False
    if base in _NON_CRYPTO:
        return False
    if _LEVERAGED.search(symbol):
        return False
    if _NUMERIC_PREFIX.match(base):
        return False
    return True


def _fetch_top_pairs(n: int, exclude: Optional[set] = None) -> List[str]:
    """
    Fetch all KuCoin tickers, sort by 24h USDT volume, and return up to n
    pairs (BTC/USDT format) that clear every dynamic-addition gate:
      - tradeable (no stables/leveraged/non-crypto)         [_is_tradeable]
      - >= MIN_VOLUME_USDT 24h volume
      - not in `exclude` (the always-scanned core)
      - not blacklisted (manual manipulation catch)
      - not data-poor (failed a prior history check)
      - established: >= MIN_PAIR_AGE_DAYS of history          [_is_established]

    The age check costs one fetch per never-seen candidate, so the loop stops
    as soon as n slots are filled.
    """
    try:
        _throttle()
        resp = requests.get(f"{_KC_URL}/market/allTickers", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "200000":
            print(f"[exchange] KuCoin allTickers error: {data.get('msg')}")
            return []

        tickers = data["data"]["ticker"]
        candidates = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not _is_tradeable(sym):
                continue
            try:
                vol = float(t.get("volValue") or 0)
            except ValueError:
                continue
            if vol < config.MIN_VOLUME_USDT:
                continue
            candidates.append((sym, vol))

        candidates.sort(key=lambda x: x[1], reverse=True)

        # Prune expired data-poor entries, then exclude the rest for up to 24h
        now_ts = time.time()
        for sym in list(_data_poor_pairs.keys()):
            if _data_poor_pairs[sym] <= now_ts:
                del _data_poor_pairs[sym]
        excluded = set(_data_poor_pairs.keys())
        if excluded:
            print(f"[exchange] Excluding {len(excluded)} data-poor pair(s): {', '.join(sorted(excluded))}")

        exclude = exclude or set()
        results: List[str] = []
        for sym, _ in candidates:
            if len(results) >= n:
                break
            pair = sym.replace("-", "/", 1)
            if pair in excluded:                  # data-poor (failed history check)
                continue
            if pair in exclude:                   # already in the scanned core
                continue
            if pair in config.PAIR_BLACKLIST:     # known-manipulated
                continue
            if not _is_established(pair):         # fresh pump listing
                continue
            results.append(pair)

        return results

    except Exception as e:
        print(f"[exchange] Failed to fetch dynamic watchlist: {e}")
        return []


def _kucoin_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV from KuCoin public API — no auth needed.
    KuCoin returns newest-first; columns: [time_sec, open, close, high, low, vol, amount].
    """
    kc_sym   = _to_kc(symbol)
    interval = _TF_MAP.get(tf, tf)
    for attempt in range(3):
        try:
            _throttle()
            resp = requests.get(
                f"{_KC_URL}/market/candles",
                params={"symbol": kc_sym, "type": interval},
                timeout=15,
            )
            if resp.status_code == 429:
                wait = 2 ** attempt * 2
                print(f"[exchange] 429 on {symbol} {tf} — backing off {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "200000":
                print(f"[exchange] KuCoin OHLCV {symbol} {tf}: {data.get('msg')}")
                return None
            rows = list(reversed(data["data"]))   # oldest-first
            if not rows:
                return None
            # KuCoin columns: time, open, close, high, low, volume, amount
            df = pd.DataFrame(rows, columns=["time", "open", "close", "high", "low", "volume", "amount"])
            df = df[["time", "open", "high", "low", "close", "volume"]].astype(
                {"time": "int64", "open": float, "high": float,
                 "low": float, "close": float, "volume": float}
            )
            df["time"] = pd.to_datetime(df["time"], unit="s")
            # KuCoin returns up to 1500 bars; trim to requested limit
            return df.tail(limit).reset_index(drop=True)
        except Exception as e:
            if attempt == 2:
                print(f"[exchange] KuCoin OHLCV {symbol} {tf}: {e}")
            else:
                time.sleep(1)
    return None


def _kucoin_price(symbol: str) -> Optional[float]:
    kc_sym = _to_kc(symbol)
    try:
        _throttle()
        resp = requests.get(
            f"{_KC_URL}/market/orderbook/level1",
            params={"symbol": kc_sym},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "200000":
            print(f"[exchange] KuCoin price {symbol}: {data.get('msg')}")
            return None
        return float(data["data"]["price"])
    except Exception as e:
        print(f"[exchange] KuCoin price {symbol}: {e}")
        return None


def fetch_ohlcv_between(
    symbol: str, tf: str, start_ts: int, end_ts: int, limit: int = 200
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV for a historical time range using KuCoin startAt/endAt params.
    start_ts / end_ts are Unix timestamps (seconds, UTC).
    Used by the analytics outcome worker to walk historical candles.
    """
    kc_sym   = _to_kc(symbol)
    interval = _TF_MAP.get(tf, tf)
    last = _RANGE_RETRIES - 1
    for attempt in range(_RANGE_RETRIES):
        try:
            _throttle()
            resp = requests.get(
                f"{_KC_URL}/market/candles",
                params={
                    "symbol":  kc_sym,
                    "type":    interval,
                    "startAt": start_ts,
                    "endAt":   end_ts,
                },
                timeout=15,
            )
            if resp.status_code == 429:
                if attempt < last:
                    wait = min(2 ** attempt * 2, _RANGE_BACKOFF_CAP)
                    print(f"[exchange] 429 on {symbol} {tf} range — backing off {wait:.0f}s")
                    time.sleep(wait)
                    continue
                print(f"[exchange] 429 on {symbol} {tf} range — gave up after {_RANGE_RETRIES} tries")
                return None
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "200000":
                print(f"[exchange] KuCoin OHLCV range {symbol} {tf}: {data.get('msg')}")
                return None
            rows = list(reversed(data["data"]))   # oldest-first
            if not rows:
                # Empty under a burst is a soft-throttle, not a truly empty range —
                # back off and retry rather than silently returning None.
                if attempt < last:
                    wait = min(2 ** attempt * 2, _RANGE_BACKOFF_CAP)
                    time.sleep(wait)
                    continue
                return None
            df = pd.DataFrame(rows, columns=["time", "open", "close", "high", "low", "volume", "amount"])
            df = df[["time", "open", "high", "low", "close", "volume"]].astype(
                {"time": "int64", "open": float, "high": float,
                 "low": float, "close": float, "volume": float}
            )
            df["time"] = pd.to_datetime(df["time"], unit="s")
            return df.tail(limit).reset_index(drop=True)
        except Exception as e:
            if attempt == last:
                print(f"[exchange] KuCoin OHLCV range {symbol} {tf}: {e}")
            else:
                time.sleep(1)
    return None


def fetch_ohlcv(symbol: str, tf: str, limit: int = 250) -> Optional[pd.DataFrame]:
    key = f"{symbol}_{tf}"
    now = time.time()
    ttl = _TF_TTL.get(tf, 60)

    if key in _ohlcv_cache and (now - _ohlcv_ts.get(key, 0)) < ttl:
        return _ohlcv_cache[key]

    df = _kucoin_ohlcv(symbol, tf, limit)
    if df is not None:
        _ohlcv_cache[key] = df
        _ohlcv_ts[key]    = now
    return df


def clear_cache():
    _ohlcv_cache.clear()
    _ohlcv_ts.clear()


def get_spot_price(symbol: str) -> Optional[float]:
    return _kucoin_price(symbol)


def get_watchlist() -> List[str]:
    """
    Hybrid watchlist = curated core (always) + dynamically-discovered established,
    liquid pairs, up to MAX_PAIRS total. Refreshed every 4h.

    The core (config.WATCHLIST) is always scanned regardless of its KuCoin volume,
    so thin-but-blue-chip majors never drop out. With DYNAMIC_DISCOVERY off, only
    the core is scanned.
    """
    global _watchlist_cache, _watchlist_ts

    core = config.WATCHLIST[: config.MAX_PAIRS]

    if not config.DYNAMIC_DISCOVERY:
        return core

    now = time.time()
    if _watchlist_cache and (now - _watchlist_ts) < _WATCHLIST_TTL:
        return _watchlist_cache

    slots = config.MAX_PAIRS - len(core)
    dynamic = _fetch_top_pairs(slots, exclude=set(core)) if slots > 0 else []
    result = core + [p for p in dynamic if p not in core]

    if dynamic:
        _watchlist_cache = result
        _watchlist_ts    = now
        print(
            f"[exchange] Watchlist refreshed: {len(core)} core + {len(result) - len(core)} "
            f"dynamic (≥${config.MIN_VOLUME_USDT/1e6:.0f}M, ≥{config.MIN_PAIR_AGE_DAYS}d history)"
        )
    elif not _watchlist_cache:
        # Dynamic fetch failed or added nothing — scan the core alone.
        _watchlist_cache = core
        _watchlist_ts    = now

    return _watchlist_cache
