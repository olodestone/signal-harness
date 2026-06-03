"""
STRATEGY STUB — plug your own signal logic in here.

This is the ONE file the harness leaves empty for you. Everything else
(coin universe, OHLCV fetching, signal logging, /edge + /diagnose, Telegram)
is done for you. You decide direction / entry / SL / TP / score; the harness
logs it and the analytics replay tells you whether the setup would have won.

Contract:
  generate_signal(df_entry, df_confirm, df_trend, df_bias, bar_time=None)
    is called once per pair per scan. Return a Signal for the best setup,
    or None when there's nothing.

  Each df is an OHLCV DataFrame with columns:
      timestamp, open, high, low, close, volume
  for config.ENTRY_TF / CONFIRM_TF / TREND_TF / BIAS_TF respectively.

Out of scope (intentionally): execution, order sizing, risk. The harness
measures SIGNAL QUALITY only. Don't place orders from here.

This stub returns None, so the harness runs end-to-end out of the box and
simply reports "0 signals" every scan until you implement _detect().
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd  # noqa: F401 — your detection logic will use it

import config  # noqa: F401 — read thresholds like config.MIN_CONFIDENCE here


@dataclass
class Signal:
    """The exact shape the harness, notifier, and /edge + /diagnose expect.

    Do not rename fields — analytics groups on `direction`, `regime`,
    `session`, `quality`, and the confidence bands.
    """
    direction:     str           # "BUY" | "SELL"
    entry:         float         # intended entry price
    current_price: float         # close price when the signal was detected
    sl:            float         # stop loss
    tp:            float         # take profit (TP1) — used by the 72h setup replay
    rr:            float         # reward : risk
    strategy:      str           # free-text label, e.g. "my_breakout_v1"
    confidence:    int           # 1-100 (drives /edge conf bands + quality)
    quality:       str           # "high" | "medium" | "low"
    regime:        str           # "trending_up" | "trending_down" | "ranging"
    session:       str           # "london_ny" | "london" | "new_york" | "asian"
    atr:           float = 0.0   # ATR at signal time (stored for ATR-bucket analytics)


def session_for(ts: Optional[datetime] = None) -> str:
    """UTC session classifier — generic market knowledge, not strategy.

    Provided as a convenience so your Signal.session matches what /edge expects.
    Windows: Asian 00-07, London 07-16, London/NY overlap 13-16, New York 16-22.
    """
    h = (ts or datetime.now(timezone.utc)).hour
    if 13 <= h < 16:
        return "london_ny"
    if 7 <= h < 16:
        return "london"
    if 16 <= h < 22:
        return "new_york"
    return "asian"


def generate_signal(
    df_entry:   "pd.DataFrame",
    df_confirm: "pd.DataFrame",
    df_trend:   "pd.DataFrame",
    df_bias:    "pd.DataFrame",
    bar_time:   Optional[datetime] = None,
) -> Optional[Signal]:
    """Validated config (see backtest/results_oos.md): bt_trend + bt_sweep on the 4h
    entry stack (set ENTRY_TF=4h / CONFIRM_TF=1d / TREND_TF=1d / BIAS_TF=1d). Both passed
    out-of-sample. Runs both candidates and returns the higher-confidence signal.

    NOTE: the harness scores a FIXED entry/SL/TP setup, which for these is ~breakeven. The
    validated *edge* is in the TRAILING exit — measure it by periodically running
    `python -m backtest.exit_lab --strategies <RUN_TAG>` over the live-logged signals.

    Lazy imports avoid a circular import (backtest.strategies imports Signal from here).
    The last (still-forming) candle of each frame is dropped so the live decision bar is the
    last CLOSED bar — matching the backtest's no-lookahead semantics.
    """
    from backtest.indicators import add_indicator_columns
    from backtest.strategies import gen_trend, gen_sweep

    if any(df is None or len(df) < 30 for df in (df_entry, df_confirm, df_trend, df_bias)):
        return None
    de = add_indicator_columns(df_entry.iloc[:-1])    # drop forming bar → last bar is closed
    dc = add_indicator_columns(df_confirm.iloc[:-1])
    dt = add_indicator_columns(df_trend.iloc[:-1])
    db = add_indicator_columns(df_bias.iloc[:-1])

    candidates = []
    for fn in (gen_trend, gen_sweep):
        try:
            sig = fn(de, dc, dt, db, bar_time=bar_time)
        except Exception:
            sig = None
        if sig is not None and sig.confidence >= config.MIN_CONFIDENCE:
            candidates.append(sig)

    if not candidates:
        return None
    return max(candidates, key=lambda s: s.confidence)
