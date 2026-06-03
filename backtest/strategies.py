"""
Four candidate strategies, each shaped exactly like the production
`strategy.generate_signal` contract so they could be dropped into the live harness
unchanged. They return the real `strategy.Signal` (imported, not redefined).

Common rules enforced by `_finalize`:
  - entry within `config.MAX_ENTRY_DISTANCE_PCT` of market
  - `config.MIN_RR <= rr <= config.MAX_RR`  (TP clamped/validated)
  - confidence gate `config.MIN_CONFIDENCE`
  - direction-consistent stop (SL below entry for BUY, above for SELL)

`atr`, `regime`, `session` are set so /edge + /diagnose group them correctly.

REGISTRY maps run_tag -> (function, trade_type_label) and is what the driver iterates.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

import config
from strategy import Signal, session_for

from .indicators import (
    sr_zones, nearest_zone_above, nearest_zone_below, confirmed_swings,
    bullish_reversal, bearish_reversal, candle_strength,
    hammer, shooting_star, bullish_engulfing, bearish_engulfing,
)


# ── shared helpers ───────────────────────────────────────────────────────────

def _regime(df_trend: pd.DataFrame) -> str:
    last = df_trend.iloc[-1]
    e50, e200, c = float(last["ema50"]), float(last["ema200"]), float(last["close"])
    if e50 > e200 and c > e50:
        return "trending_up"
    if e50 < e200 and c < e50:
        return "trending_down"
    return "ranging"


def _bias(df_bias: pd.DataFrame) -> str:
    last = df_bias.iloc[-1]
    c, e50 = float(last["close"]), float(last["ema50"])
    if c > e50:
        return "up"
    if c < e50:
        return "down"
    return "flat"


def _align_bonus(direction: str, regime: str) -> float:
    if direction == "BUY":
        return {"trending_up": 12.0, "ranging": 3.0, "trending_down": -14.0}[regime]
    return {"trending_down": 12.0, "ranging": 3.0, "trending_up": -14.0}[regime]


def _finalize(
    *, label: str, direction: str, entry: float, sl: float, current_price: float,
    atr: float, confidence: float, regime: str, bar_time: Optional[datetime],
    desired_rr: float = 2.0, struct_tp: Optional[float] = None,
) -> Optional[Signal]:
    if entry <= 0 or atr <= 0 or current_price <= 0:
        return None
    if direction == "BUY"  and not sl < entry:
        return None
    if direction == "SELL" and not sl > entry:
        return None

    risk = abs(entry - sl)
    if risk <= 0:
        return None
    if abs(entry - current_price) / current_price > config.MAX_ENTRY_DISTANCE_PCT:
        return None

    sign = 1.0 if direction == "BUY" else -1.0
    tp = entry + sign * desired_rr * risk
    if struct_tp is not None and (struct_tp - entry) * sign > 0:      # struct target on the right side
        if abs(struct_tp - entry) / risk >= config.MIN_RR:
            tp = struct_tp

    rr = abs(tp - entry) / risk
    if rr > config.MAX_RR:
        tp = entry + sign * config.MAX_RR * risk
        rr = config.MAX_RR
    if rr < config.MIN_RR:
        return None

    conf = int(max(1, min(99, round(confidence))))
    if conf < config.MIN_CONFIDENCE:
        return None
    quality = "high" if conf >= 80 else ("medium" if conf >= 65 else "low")

    return Signal(
        direction=direction, entry=float(entry), current_price=float(current_price),
        sl=float(sl), tp=float(tp), rr=round(float(rr), 2), strategy=label,
        confidence=conf, quality=quality, regime=regime,
        session=session_for(bar_time), atr=float(atr),
    )


def _ctx(df_entry: pd.DataFrame):
    last = df_entry.iloc[-1]
    return (
        float(last["close"]), float(last["high"]), float(last["low"]),
        float(last["atr14"]), float(last["ema20"]), float(last["ema50"]),
    )


# ── 1. Support / Resistance rejection ────────────────────────────────────────

def gen_sr(df_entry, df_confirm, df_trend, df_bias, bar_time=None) -> Optional[Signal]:
    price, high, low, atr, _, _ = _ctx(df_entry)
    if atr <= 0:
        return None
    regime = _regime(df_trend)
    supports = sr_zones(df_confirm, "low", 0.005)
    resists  = sr_zones(df_confirm, "high", 0.005)

    # BUY: bar wicked into a support zone and closed back above with a bullish candle
    if bullish_reversal(df_entry):
        touched = [z for z in supports if low <= z[0] * 1.003 and price > z[0]]
        if touched:
            level, touches = max(touched, key=lambda z: z[0])
            sl = level - 0.6 * atr
            struct_tp = nearest_zone_above(resists, price)
            conf = 62 + _align_bonus("BUY", regime) + min(touches, 3) * 3 \
                + candle_strength(df_entry.iloc[-1]) * 14
            return _finalize(label="SR_REJECTION", direction="BUY", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, struct_tp=struct_tp[0] if struct_tp else None)

    # SELL: bar wicked into a resistance zone and closed back below with a bearish candle
    if bearish_reversal(df_entry):
        touched = [z for z in resists if high >= z[0] * 0.997 and price < z[0]]
        if touched:
            level, touches = min(touched, key=lambda z: z[0])
            sl = level + 0.6 * atr
            struct_tp = nearest_zone_below(supports, price)
            conf = 62 + _align_bonus("SELL", regime) + min(touches, 3) * 3 \
                + candle_strength(df_entry.iloc[-1]) * 14
            return _finalize(label="SR_REJECTION", direction="SELL", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, struct_tp=struct_tp[0] if struct_tp else None)
    return None


# ── 2. Market-structure break + retest (BOS) ─────────────────────────────────

def gen_bos(df_entry, df_confirm, df_trend, df_bias, bar_time=None) -> Optional[Signal]:
    price, high, low, atr, _, _ = _ctx(df_entry)
    if atr <= 0:
        return None
    regime = _regime(df_trend)
    sh = confirmed_swings(df_confirm, "high")
    sl_sw = confirmed_swings(df_confirm, "low")
    if not sh or not sl_sw:
        return None
    closes = df_confirm["close"].values

    # Bullish BOS: a recent 1h close broke the latest swing high; now retesting it
    last_sh = sh[-1][1]
    if closes[-6:].max() > last_sh and last_sh * 0.996 <= price <= last_sh * 1.006:
        if bullish_reversal(df_entry):
            base_sl = sl_sw[-1][1]
            sl = (base_sl - 0.3 * atr) if base_sl < price else (price - 1.5 * atr)
            conf = 64 + _align_bonus("BUY", regime) + candle_strength(df_entry.iloc[-1]) * 14
            return _finalize(label="BOS_RETEST", direction="BUY", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)

    # Bearish BOS: a recent 1h close broke the latest swing low; now retesting it
    last_sl = sl_sw[-1][1]
    if closes[-6:].min() < last_sl and last_sl * 0.994 <= price <= last_sl * 1.004:
        if bearish_reversal(df_entry):
            base_sl = sh[-1][1]
            sl = (base_sl + 0.3 * atr) if base_sl > price else (price + 1.5 * atr)
            conf = 64 + _align_bonus("SELL", regime) + candle_strength(df_entry.iloc[-1]) * 14
            return _finalize(label="BOS_RETEST", direction="SELL", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)
    return None


# ── 3. Trend pullback (EMA + candle) ─────────────────────────────────────────

def gen_trend(df_entry, df_confirm, df_trend, df_bias, bar_time=None) -> Optional[Signal]:
    price, high, low, atr, e20, e50 = _ctx(df_entry)
    if atr <= 0:
        return None
    regime = _regime(df_trend)
    bias = _bias(df_bias)

    # Long: 1d + 4h both up, 15m pulled back to the EMA20/50 band, bullish reversal
    if bias == "up" and regime == "trending_up":
        if low <= e20 and price > e20 * 0.999 and low >= e50 * 0.995 and bullish_reversal(df_entry):
            sl = min(e50, low) - 0.3 * atr
            conf = 70 + candle_strength(df_entry.iloc[-1]) * 12 + (4 if price > e20 else 0)
            return _finalize(label="TREND_PULLBACK", direction="BUY", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)

    # Short: 1d + 4h both down, 15m rallied to the EMA20/50 band, bearish reversal
    if bias == "down" and regime == "trending_down":
        if high >= e20 and price < e20 * 1.001 and high <= e50 * 1.005 and bearish_reversal(df_entry):
            sl = max(e50, high) + 0.3 * atr
            conf = 70 + candle_strength(df_entry.iloc[-1]) * 12 + (4 if price < e20 else 0)
            return _finalize(label="TREND_PULLBACK", direction="SELL", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)
    return None


# ── 4. Liquidity sweep + breaker (ICT) ───────────────────────────────────────

def gen_sweep(df_entry, df_confirm, df_trend, df_bias, bar_time=None) -> Optional[Signal]:
    price, high, low, atr, _, _ = _ctx(df_entry)
    if atr <= 0:
        return None
    regime = _regime(df_trend)
    sl_sw = confirmed_swings(df_confirm, "low")
    sh_sw = confirmed_swings(df_confirm, "high")
    recent = df_confirm.iloc[-4:]

    # Bullish: recent 1h bar swept below a swing low then reclaimed it; enter on reclaim
    if sl_sw:
        liq = sl_sw[-1][1]
        swept = bool(((recent["low"] < liq) & (recent["close"] > liq)).any())
        if swept and price > liq and bullish_reversal(df_entry):
            sweep_low = float(recent["low"].min())
            sl = sweep_low - 0.3 * atr
            conf = 66 + _align_bonus("BUY", regime) + candle_strength(df_entry.iloc[-1]) * 12
            return _finalize(label="ICT_SWEEP_BREAKER", direction="BUY", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)

    # Bearish: recent 1h bar swept above a swing high then reclaimed below; enter short
    if sh_sw:
        liq = sh_sw[-1][1]
        swept = bool(((recent["high"] > liq) & (recent["close"] < liq)).any())
        if swept and price < liq and bearish_reversal(df_entry):
            sweep_high = float(recent["high"].max())
            sl = sweep_high + 0.3 * atr
            conf = 66 + _align_bonus("SELL", regime) + candle_strength(df_entry.iloc[-1]) * 12
            return _finalize(label="ICT_SWEEP_BREAKER", direction="SELL", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)
    return None


# ── 5. Trend pullback v2 — tighter confluence ────────────────────────────────
# Over v1 adds: triple-TF alignment (1d bias + 4h regime + 1h trend), a *strong*
# reversal candle (engulfing, or hammer/star with body-decisive wick), horizontal
# S/R confluence at the pullback, and a volatility band (skip dead & chaotic ATR).

def gen_trend2(df_entry, df_confirm, df_trend, df_bias, bar_time=None) -> Optional[Signal]:
    price, high, low, atr, e20, e50 = _ctx(df_entry)
    if atr <= 0:
        return None
    atr_pct = atr / price
    if not (0.0015 <= atr_pct <= 0.015):        # skip dead & chaotic (15m ATR% ~0.2-0.5% typical)
        return None
    regime = _regime(df_trend)
    bias = _bias(df_bias)
    c1 = df_confirm.iloc[-1]
    up1h = float(c1["close"]) > float(c1["ema50"]) and float(c1["ema20"]) > float(c1["ema50"])
    dn1h = float(c1["close"]) < float(c1["ema50"]) and float(c1["ema20"]) < float(c1["ema50"])
    strength = candle_strength(df_entry.iloc[-1])
    supports = sr_zones(df_confirm, "low", 0.005)
    resists  = sr_zones(df_confirm, "high", 0.005)

    if bias == "up" and regime == "trending_up" and up1h:
        pulled = low <= e20 and low >= e50 * 0.995 and price > e20 * 0.999
        strong = bullish_engulfing(df_entry) or (hammer(df_entry.iloc[-1]) and strength >= 0.55)
        near_sr = any(z[0] * 0.996 <= low <= z[0] * 1.004 for z in supports)
        if pulled and strong and (near_sr or low <= e50 * 1.002):
            sl = min(e50, low) - 0.3 * atr
            conf = 76 + strength * 14 + (4 if near_sr else 0)
            return _finalize(label="TREND_PULLBACK2", direction="BUY", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)

    if bias == "down" and regime == "trending_down" and dn1h:
        pulled = high >= e20 and high <= e50 * 1.005 and price < e20 * 1.001
        strong = bearish_engulfing(df_entry) or (shooting_star(df_entry.iloc[-1]) and strength >= 0.55)
        near_sr = any(z[0] * 0.996 <= high <= z[0] * 1.004 for z in resists)
        if pulled and strong and (near_sr or high >= e50 * 0.998):
            sl = max(e50, high) + 0.3 * atr
            conf = 76 + strength * 14 + (4 if near_sr else 0)
            return _finalize(label="TREND_PULLBACK2", direction="SELL", entry=price, sl=sl,
                             current_price=price, atr=atr, confidence=conf, regime=regime,
                             bar_time=bar_time, desired_rr=2.0)
    return None


# ── 6. Liquidity sweep v2 — tighter ──────────────────────────────────────────
# Over v1 adds: only sweeps of a *multi-touch* (>=2) liquidity level, a meaningful
# displacement (pierce >=0.15·ATR, reclaim >=0.10·ATR), higher-TF alignment (no
# counter-trend sweeps), a strong reclaim candle, and a volatility band.

def gen_sweep2(df_entry, df_confirm, df_trend, df_bias, bar_time=None) -> Optional[Signal]:
    price, high, low, atr, _, _ = _ctx(df_entry)
    if atr <= 0:
        return None
    atr_pct = atr / price
    if not (0.0015 <= atr_pct <= 0.02):
        return None
    regime = _regime(df_trend)
    strength = candle_strength(df_entry.iloc[-1])
    recent = df_confirm.iloc[-4:]

    if regime != "trending_down":               # bullish sweep, with/neutral HTF bias
        below = [z[0] for z in sr_zones(df_confirm, "low", 0.005) if z[1] >= 2 and z[0] < price]
        if below:
            liq = max(below)
            pierced = bool(((recent["low"] < liq - 0.15 * atr) & (recent["close"] > liq + 0.10 * atr)).any())
            if pierced and price > liq and bullish_reversal(df_entry) and strength >= 0.5:
                sl = float(recent["low"].min()) - 0.3 * atr
                conf = 74 + strength * 16 + _align_bonus("BUY", regime) * 0.5
                return _finalize(label="ICT_SWEEP2", direction="BUY", entry=price, sl=sl,
                                 current_price=price, atr=atr, confidence=conf, regime=regime,
                                 bar_time=bar_time, desired_rr=2.0)

    if regime != "trending_up":                 # bearish sweep
        above = [z[0] for z in sr_zones(df_confirm, "high", 0.005) if z[1] >= 2 and z[0] > price]
        if above:
            liq = min(above)
            pierced = bool(((recent["high"] > liq + 0.15 * atr) & (recent["close"] < liq - 0.10 * atr)).any())
            if pierced and price < liq and bearish_reversal(df_entry) and strength >= 0.5:
                sl = float(recent["high"].max()) + 0.3 * atr
                conf = 74 + strength * 16 + _align_bonus("SELL", regime) * 0.5
                return _finalize(label="ICT_SWEEP2", direction="SELL", entry=price, sl=sl,
                                 current_price=price, atr=atr, confidence=conf, regime=regime,
                                 bar_time=bar_time, desired_rr=2.0)
    return None


# run_tag -> (generate_fn, trade_type label stored on each signal)
REGISTRY = {
    "bt_sr":     (gen_sr,     "SR_REJECTION"),
    "bt_bos":    (gen_bos,    "BOS_RETEST"),
    "bt_trend":  (gen_trend,  "TREND_PULLBACK"),
    "bt_sweep":  (gen_sweep,  "ICT_SWEEP_BREAKER"),
    "bt_trend2": (gen_trend2, "TREND_PULLBACK2"),
    "bt_sweep2": (gen_sweep2, "ICT_SWEEP2"),
}
