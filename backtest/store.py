"""
In-memory OHLCV store for the replay.

Fetches each pair's full history ONCE per timeframe (paginating 15m, which exceeds
KuCoin's ~1500-bar-per-request cap over 30d+warmup), precomputes causal indicator
columns, and serves three lookahead-safe views used by the driver:

  as_of(pair, tf, decision_ts)  → last 250 bars that have *closed* by decision_ts
  between(pair, tf, t0, t1, …)  → bars in [t0, t1]  (signature-compatible with
                                   exchange.fetch_ohlcv_between, for the resolver monkeypatch)
  price_at(pair, target_ts)     → close of the 15m candle covering target_ts (4h/24h snaps)

A bar with open time `t` is only usable at decision time `D` once it has fully
closed, i.e. `t + tf_seconds <= D`. All slicing enforces that.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import exchange as ex
from .indicators import add_indicator_columns

TF_SECONDS: Dict[str, int] = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
ALL_TFS = ("15m", "1h", "4h", "1d")

# Bars of history to keep BEFORE the replay window so EMA200 etc. are warm at step 1.
WARMUP_BARS = 220
# KuCoin returns up to ~1500 candles/request; stay under it per page.
_PAGE_BARS = 1400


class OHLCVStore:
    def __init__(self, days: int, warmup_bars: int = WARMUP_BARS):
        self.days = days
        self.warmup_bars = warmup_bars
        # pair -> tf -> {"df": DataFrame(+indicator cols), "t": int64 open-time array}
        self._data: Dict[str, Dict[str, dict]] = {}

    # ── ingestion ────────────────────────────────────────────────────────────
    def load(self, pair: str, window_start: int, window_end: int, tfs=ALL_TFS) -> bool:
        """Fetch + precompute the given timeframes for `pair`. Returns False if any
        requested TF is too thin to be useful (caller should skip the pair).
        Pass tfs=("15m",) for exit-policy walks that only need the entry timeframe."""
        per_tf: Dict[str, dict] = {}
        for tf in tfs:
            tf_sec = TF_SECONDS[tf]
            fetch_start = window_start - self.warmup_bars * tf_sec
            df = self._fetch_full(pair, tf, fetch_start, window_end)
            if df is None or len(df) < 60:
                return False
            df = add_indicator_columns(df)
            per_tf[tf] = {
                "df": df.reset_index(drop=True),
                "t": (df["time"].astype("int64") // 10**9).to_numpy(),
            }
        self._data[pair] = per_tf
        return True

    def _fetch_full(self, pair: str, tf: str, start_ts: int, end_ts: int) -> Optional[pd.DataFrame]:
        """Paginate exchange.fetch_ohlcv_between across the full range, oldest-first."""
        tf_sec = TF_SECONDS[tf]
        span = _PAGE_BARS * tf_sec
        frames: List[pd.DataFrame] = []
        cur = start_ts
        while cur < end_ts:
            w_end = min(cur + span, end_ts)
            df = ex.fetch_ohlcv_between(pair, tf, cur, w_end, limit=1500)
            if df is not None and len(df):
                frames.append(df)
            cur = w_end
        if not frames:
            return None
        full = pd.concat(frames, ignore_index=True)
        full = full.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
        return full

    # ── views ────────────────────────────────────────────────────────────────
    def as_of(self, pair: str, tf: str, decision_ts: int, limit: int = 250) -> Optional[pd.DataFrame]:
        """Last `limit` bars of `tf` fully closed by `decision_ts` (no lookahead)."""
        node = self._data.get(pair, {}).get(tf)
        if node is None:
            return None
        cutoff = decision_ts - TF_SECONDS[tf]          # open-time <= cutoff ⇒ closed by decision_ts
        idx = int(np.searchsorted(node["t"], cutoff, side="right"))
        if idx <= 0:
            return None
        lo = max(0, idx - limit)
        return node["df"].iloc[lo:idx]

    def between(self, pair: str, tf: str, start_ts: int, end_ts: int, limit: int = 200) -> Optional[pd.DataFrame]:
        """Bars with open-time in [start_ts, end_ts], oldest-first, tail(limit).

        Argument order matches exchange.fetch_ohlcv_between so the resolver can call
        this transparently after a monkeypatch.
        """
        node = self._data.get(pair, {}).get(tf)
        if node is None:
            return None
        t = node["t"]
        lo = int(np.searchsorted(t, start_ts, side="left"))
        hi = int(np.searchsorted(t, end_ts, side="right"))
        if hi <= lo:
            return None
        sl = node["df"].iloc[lo:hi]
        return sl.tail(limit)

    def price_at(self, pair: str, target_ts: int, tf: str = "15m") -> Optional[float]:
        """Close of the `tf` candle covering `target_ts`, or None if beyond data."""
        node = self._data.get(pair, {}).get(tf)
        if node is None:
            return None
        t = node["t"]
        idx = int(np.searchsorted(t, target_ts, side="right")) - 1
        if idx < 0 or target_ts > int(t[-1]) + TF_SECONDS[tf]:
            return None
        return float(node["df"]["close"].iloc[idx])

    def decision_times(self, pair: str, window_start: int, window_end: int,
                       tf: str = "15m") -> List[int]:
        """Close timestamps of `tf` candles whose close falls in (window_start, window_end]."""
        node = self._data.get(pair, {}).get(tf)
        if node is None:
            return []
        closes = node["t"] + TF_SECONDS[tf]
        mask = (closes > window_start) & (closes <= window_end)
        return [int(x) for x in closes[mask]]


def now_ts() -> int:
    return int(time.time())
