"""
Shared, dependency-free (pandas/numpy only) technical helpers for the backtest
candidate strategies.

Two kinds of helpers live here:

1. Column builders (`add_indicator_columns`) — causal indicators (EMA/ATR) plus
   swing-pivot flags. These are precomputed ONCE per pair/timeframe by the store and
   sliced per replay step, so strategies never recompute them in the hot loop.

   Swing flags use a centered window of `SWING_W` bars on each side, so a swing at
   bar i is only *confirmed* at bar i+SWING_W. Consumers MUST ignore the last
   SWING_W bars of any slice (use `confirmed_swings`) to stay lookahead-free.

2. Candlestick detectors — operate on the last (just-closed) bar of a slice.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
import pandas as pd

# Swing confirmation half-window. A swing high/low at index i needs SWING_W bars on
# each side; it is therefore only knowable SWING_W bars later. Used by both the
# precompute (centered rolling) and the consumer (`confirmed_swings`) so they agree.
SWING_W = 2


# ── Column builders ───────────────────────────────────────────────────────────

def add_indicator_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with ema20/ema50/ema200, atr14, swing_high/swing_low.

    EMA/ATR are causal (value at i uses only bars <= i). Swing flags are centered
    and must be consumed via `confirmed_swings`.
    """
    out = df.copy()
    close = out["close"]
    out["ema20"]  = close.ewm(span=20,  adjust=False).mean()
    out["ema50"]  = close.ewm(span=50,  adjust=False).mean()
    out["ema200"] = close.ewm(span=200, adjust=False).mean()
    out["atr14"]  = _atr(out, 14)

    win = 2 * SWING_W + 1
    roll_hi = out["high"].rolling(win, center=True).max()
    roll_lo = out["low"].rolling(win, center=True).min()
    # `== window extreme` marks local pivots; edges are NaN (not pivots).
    out["swing_high"] = (out["high"] >= roll_hi) & roll_hi.notna()
    out["swing_low"]  = (out["low"]  <= roll_lo) & roll_lo.notna()
    return out


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


# ── Swing / S-R helpers ─────────────────────────────────────────────────────────

def confirmed_swings(df: pd.DataFrame, kind: str) -> List[Tuple[int, float]]:
    """Return [(iloc_index, price), …] for confirmed swing highs/lows in `df`.

    Drops the trailing SWING_W bars whose swing status can't yet be known at the
    slice's last bar — this is what keeps the replay lookahead-free.
    `kind` is 'high' or 'low'.
    """
    col   = "swing_high" if kind == "high" else "swing_low"
    price = "high"       if kind == "high" else "low"
    cutoff = len(df) - SWING_W            # exclusive upper bound on confirmable bars
    if cutoff <= 0:
        return []
    flags  = df[col].values[:cutoff]
    idxs   = np.flatnonzero(flags)        # vectorised; avoids a per-bar python loop
    prices = df[price].values[idxs]
    return list(zip(idxs.tolist(), prices.tolist()))


def sr_zones(df: pd.DataFrame, kind: str, tol: float) -> List[Tuple[float, int]]:
    """Cluster confirmed swing levels into S/R zones.

    Returns [(level, touches), …] sorted by level ascending. `tol` is the fractional
    width that merges nearby swings into one zone (e.g. 0.004 = 0.4%).
    """
    swings = [p for _, p in confirmed_swings(df, kind)]
    if not swings:
        return []
    swings.sort()
    zones: List[Tuple[float, int]] = []   # (running mean level, touch count)
    cluster = [swings[0]]
    for p in swings[1:]:
        if abs(p - cluster[-1]) / cluster[-1] <= tol:
            cluster.append(p)
        else:
            zones.append((float(np.mean(cluster)), len(cluster)))
            cluster = [p]
    zones.append((float(np.mean(cluster)), len(cluster)))
    return zones


def nearest_zone_below(zones: List[Tuple[float, int]], price: float):
    """Closest zone strictly below `price` → (level, touches) or None."""
    below = [z for z in zones if z[0] < price]
    return max(below, key=lambda z: z[0]) if below else None


def nearest_zone_above(zones: List[Tuple[float, int]], price: float):
    """Closest zone strictly above `price` → (level, touches) or None."""
    above = [z for z in zones if z[0] > price]
    return min(above, key=lambda z: z[0]) if above else None


# ── Candlestick detectors (operate on the last/just-closed bar) ──────────────────

def _ohlc(bar) -> Tuple[float, float, float, float]:
    return float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])


def _parts(o: float, h: float, l: float, c: float):
    body  = abs(c - o)
    rng   = (h - l) or 1e-12
    upper = h - max(o, c)
    lower = min(o, c) - l
    return body, rng, upper, lower


def is_bullish(bar) -> bool:
    o, _, _, c = _ohlc(bar)
    return c > o


def is_bearish(bar) -> bool:
    o, _, _, c = _ohlc(bar)
    return c < o


def hammer(bar) -> bool:
    """Long lower wick (>=2x body), small upper wick, body in the upper third."""
    o, h, l, c = _ohlc(bar)
    body, rng, upper, lower = _parts(o, h, l, c)
    return lower >= 2 * body and upper <= body and min(o, c) >= l + 0.5 * rng


def shooting_star(bar) -> bool:
    """Long upper wick, small body in the lower third — bearish rejection."""
    o, h, l, c = _ohlc(bar)
    body, rng, upper, lower = _parts(o, h, l, c)
    return upper >= 2 * body and lower <= body


def bullish_pin(bar) -> bool:
    """Lower wick dominates the range and close is in the upper half."""
    o, h, l, c = _ohlc(bar)
    body, rng, upper, lower = _parts(o, h, l, c)
    return lower >= 0.5 * rng and c >= (l + 0.5 * rng) and lower >= 1.5 * body


def bearish_pin(bar) -> bool:
    o, h, l, c = _ohlc(bar)
    body, rng, upper, lower = _parts(o, h, l, c)
    return upper >= 0.5 * rng and c <= (h - 0.5 * rng) and upper >= 1.5 * body


def bullish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    p, b = df.iloc[-2], df.iloc[-1]
    return (
        is_bearish(p) and is_bullish(b)
        and float(b["close"]) >= float(p["open"])
        and float(b["open"])  <= float(p["close"])
    )


def bearish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    p, b = df.iloc[-2], df.iloc[-1]
    return (
        is_bullish(p) and is_bearish(b)
        and float(b["close"]) <= float(p["open"])
        and float(b["open"])  >= float(p["close"])
    )


def bullish_reversal(df: pd.DataFrame) -> bool:
    """Any of: hammer / bullish pin / bullish engulfing on the last bar."""
    last = df.iloc[-1]
    return hammer(last) or bullish_pin(last) or bullish_engulfing(df)


def bearish_reversal(df: pd.DataFrame) -> bool:
    last = df.iloc[-1]
    return shooting_star(last) or bearish_pin(last) or bearish_engulfing(df)


def candle_strength(bar) -> float:
    """0..1 score of how decisive the rejection wick is (for confidence scaling)."""
    o, h, l, c = _ohlc(bar)
    body, rng, upper, lower = _parts(o, h, l, c)
    dom = max(upper, lower) / rng
    return float(min(1.0, max(0.0, dom)))
