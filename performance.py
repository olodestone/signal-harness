"""
Trade lifecycle: open → close → record.
Persists to PostgreSQL (Railway addon — DATABASE_URL injected automatically).
Supports both paper and live trade tracking.
"""
from __future__ import annotations
import os
import time
from contextlib import contextmanager
from datetime import datetime, date
from typing import Optional, List, Dict
from dataclasses import dataclass

import psycopg2
import psycopg2.extras

_DATABASE_URL = os.getenv("DATABASE_URL", "")
_CONNECT_TIMEOUT = 10  # seconds per attempt


def _conn() -> psycopg2.extensions.connection:
    if not _DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Add the Railway PostgreSQL addon to your project and redeploy."
        )
    # make_dsn bakes connect_timeout into the DSN so libpq always honours it,
    # even when _DATABASE_URL is a postgres:// URL (kwarg alone can be silently
    # ignored when a full URL string is parsed by libpq).
    dsn = psycopg2.extensions.make_dsn(_DATABASE_URL, connect_timeout=_CONNECT_TIMEOUT)
    return psycopg2.connect(dsn)


@contextmanager
def _cur():
    """Yield a RealDictCursor inside an auto-commit/rollback transaction."""
    con = _conn()
    try:
        with con:                                               # commit on success, rollback on error
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    finally:
        con.close()


# ── Trade dataclass ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    id:              int
    pair:            str
    direction:       str
    entry:           float
    sl:              float
    tp:              float
    rr:              float
    qty:             float
    risk_usd:        float
    strategy:        str
    confidence:      float
    status:          str             # "pending" | "open" | "closed_tp" | "closed_sl" | "closed_manual" | "cancelled"
    open_time:       datetime
    close_time:      Optional[datetime] = None
    close_price:     Optional[float]    = None
    pnl_usd:         Optional[float]    = None
    paper:           bool               = True
    mae_usd:         Optional[float]    = None
    mfe_usd:         Optional[float]    = None
    time_to_mae_sec: Optional[int]      = None
    time_to_mfe_sec: Optional[int]      = None
    pending_until:   Optional[datetime] = None  # expiry for pending orders
    fill_time:       Optional[datetime] = None  # when the limit order actually filled
    run_tag:         Optional[str]      = None  # config version label (v1, v2, …)


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db(base_delay: float = 5.0):
    attempt = 0
    while True:
        try:
            _init_schema()
            return
        except psycopg2.OperationalError as exc:
            attempt += 1
            wait = min(base_delay * (2 ** (attempt - 1)), 60)
            print(
                f"[perf] DB unavailable (attempt {attempt}): {exc}. "
                f"Retrying in {wait:.0f}s …",
                flush=True,
            )
            time.sleep(wait)


def _init_schema():
    with _cur() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id               SERIAL PRIMARY KEY,
                pair             TEXT,
                direction        TEXT,
                entry            REAL,
                sl               REAL,
                tp               REAL,
                rr               REAL,
                qty              REAL,
                risk_usd         REAL,
                strategy         TEXT,
                confidence       REAL,
                status           TEXT DEFAULT 'open',
                open_time        TEXT,
                close_time       TEXT,
                close_price      REAL,
                pnl_usd          REAL,
                paper            INTEGER DEFAULT 1,
                mae_usd          REAL,
                mfe_usd          REAL,
                time_to_mae_sec  INTEGER,
                time_to_mfe_sec  INTEGER
            )
        """)
        # ADD COLUMN IF NOT EXISTS is idempotent — safe on repeated startups
        for col, typedef in [
            ("mae_usd",         "REAL"),
            ("mfe_usd",         "REAL"),
            ("time_to_mae_sec", "INTEGER"),
            ("time_to_mfe_sec", "INTEGER"),
            ("pending_until",   "TEXT"),
            ("fill_time",       "TEXT"),
            ("run_tag",         "TEXT"),
            ("post_be_tp1",     "BOOLEAN"),   # did price hit TP1 after BE stop?
        ]:
            cur.execute(
                f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )

        # Signal attribution log — every signal >= 55% confidence is recorded here.
        # Tracks the full funnel: generated → placed → filled → outcome.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_log (
                id                SERIAL PRIMARY KEY,
                signal_time       TEXT NOT NULL,
                pair              TEXT NOT NULL,
                direction         TEXT NOT NULL,
                confidence        INTEGER NOT NULL,
                quality           TEXT NOT NULL,
                session           TEXT NOT NULL,
                regime            TEXT NOT NULL,
                rr                REAL NOT NULL,
                entry             REAL NOT NULL,
                sl                REAL NOT NULL,
                tp                REAL NOT NULL,
                paper             INTEGER NOT NULL DEFAULT 1,
                placed            INTEGER NOT NULL DEFAULT 0,
                skip_reason       TEXT,
                trade_id          INTEGER,
                filled            INTEGER,
                outcome           TEXT,
                pnl_usd           REAL,
                direction_correct INTEGER,
                price_at_expiry   REAL
            )
        """)
        # Analytics columns — additive, safe on repeated startups
        for col, typedef in [
            ("atr",           "REAL"),
            ("price_at_gen",  "REAL"),
            ("stage",         "TEXT DEFAULT 'queued'"),
            ("snap_4h",       "REAL"),
            ("dir_4h",        "BOOLEAN"),
            ("snap_24h",      "REAL"),
            ("dir_24h",       "BOOLEAN"),
            ("entry_reached", "BOOLEAN"),
            ("tp1_reached",   "BOOLEAN"),
            ("sl_reached",    "BOOLEAN"),
            ("setup_outcome", "TEXT"),
            ("trade_type",    "TEXT DEFAULT 'ICT_SWEEP_BREAKER'"),
            ("tp2",           "REAL"),
            ("market_type",   "TEXT DEFAULT 'spot'"),
            ("run_tag",       "TEXT"),   # config version, for old-vs-new segmentation
        ]:
            cur.execute(
                f"ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )
        # Backfill stage for existing rows that predate the analytics upgrade.
        # Order matters: filled overrides placed, expired overrides both.
        cur.execute("""
            UPDATE signal_log SET stage = 'filled'
            WHERE filled = 1 AND (stage IS NULL OR stage = 'queued')
        """)
        cur.execute("""
            UPDATE signal_log SET stage = 'expired'
            WHERE outcome = 'unfilled' AND (stage IS NULL OR stage = 'queued')
        """)
        cur.execute("""
            UPDATE signal_log SET stage = 'cancelled'
            WHERE placed = 0 AND (stage IS NULL OR stage = 'queued')
        """)
        # Stale-cancel backfill: limits killed by the 30-min FILL_TIMEOUT had their
        # trades row set status='cancelled' but the signal_log row was left
        # stage='queued' (the stale path historically skipped mark_signal_unfilled).
        # Re-stage them as 'expired' so populate_outcomes() can score the setup.
        cur.execute("""
            UPDATE signal_log s
            SET stage = 'expired', outcome = COALESCE(s.outcome, 'unfilled')
            FROM trades t
            WHERE s.trade_id = t.id
              AND t.status = 'cancelled'
              AND s.stage = 'queued'
              AND COALESCE(s.filled, 0) = 0
        """)
        # run_tag backfill: signal_log gained run_tag late, so existing rows are NULL.
        # Attribute them from the linked trade where possible, else to the pre-v4
        # regime, so /edge & /diagnose can filter old vs new. New rows are tagged at
        # insert (never NULL), so this only ever touches the historical backlog.
        cur.execute("""
            UPDATE signal_log s
            SET run_tag = t.run_tag
            FROM trades t
            WHERE s.trade_id = t.id
              AND s.run_tag IS NULL
              AND t.run_tag IS NOT NULL
        """)
        cur.execute("UPDATE signal_log SET run_tag = 'v3' WHERE run_tag IS NULL")


# ── Write operations ──────────────────────────────────────────────────────────

def open_trade(
    pair: str, direction: str, entry: float, sl: float, tp: float,
    rr: float, qty: float, risk_usd: float, strategy: str,
    confidence: float, paper: bool = True,
    status: str = "pending", pending_until: Optional[str] = None,
    run_tag: Optional[str] = None,
) -> int:
    with _cur() as cur:
        cur.execute("""
            INSERT INTO trades
              (pair,direction,entry,sl,tp,rr,qty,risk_usd,strategy,confidence,
               status,open_time,paper,pending_until,run_tag)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (pair, direction, float(entry), float(sl), float(tp), float(rr),
              float(qty), float(risk_usd), strategy, float(confidence),
              status, datetime.utcnow().isoformat(), int(paper), pending_until,
              run_tag))
        return cur.fetchone()["id"]


def close_trade(trade_id: int, close_price: float, status: str) -> Optional[Trade]:
    """
    status: "closed_tp" | "closed_sl" | "closed_manual"
    Calculates P&L from close price vs entry.
    """
    with _cur() as cur:
        cur.execute("SELECT * FROM trades WHERE id=%s", (trade_id,))
        row = cur.fetchone()
        if not row:
            return None

        pnl = (
            (close_price - row["entry"]) * row["qty"] if row["direction"] == "BUY"
            else (row["entry"] - close_price) * row["qty"]
        )

        cur.execute("""
            UPDATE trades
            SET status=%s, close_time=%s, close_price=%s, pnl_usd=%s
            WHERE id=%s
        """, (status, datetime.utcnow().isoformat(), close_price, round(pnl, 4), trade_id))

        cur.execute("SELECT * FROM trades WHERE id=%s", (trade_id,))
        return _row_to_trade(cur.fetchone())


def update_excursion(trade_id: int, current_price: float) -> None:
    """
    Track MAE and MFE for an open trade. Called every scan cycle.

    MAE (Maximum Adverse Excursion) — worst unrealized loss in USD.
    MFE (Maximum Favorable Excursion) — best unrealized gain in USD.
    Both record how many seconds elapsed from open_time when the extreme was set.
    """
    with _cur() as cur:
        cur.execute(
            "SELECT * FROM trades WHERE id=%s AND status='open'", (trade_id,)
        )
        row = cur.fetchone()
        if not row:
            return

        unrealized = (
            (current_price - row["entry"]) * row["qty"] if row["direction"] == "BUY"
            else (row["entry"] - current_price) * row["qty"]
        )

        ref_time = row.get("fill_time") or row["open_time"]
        elapsed = int((datetime.utcnow() - datetime.fromisoformat(ref_time)).total_seconds())
        updates: dict = {}

        if unrealized < 0:
            new_mae = round(abs(unrealized), 4)
            if row["mae_usd"] is None or new_mae > row["mae_usd"]:
                updates["mae_usd"]         = new_mae
                updates["time_to_mae_sec"] = elapsed

        if unrealized > 0:
            new_mfe = round(unrealized, 4)
            if row["mfe_usd"] is None or new_mfe > row["mfe_usd"]:
                updates["mfe_usd"]         = new_mfe
                updates["time_to_mfe_sec"] = elapsed

        if updates:
            cols = ", ".join(f"{k}=%s" for k in updates)
            cur.execute(
                f"UPDATE trades SET {cols} WHERE id=%s",
                [*updates.values(), trade_id],
            )


# ── Read operations ───────────────────────────────────────────────────────────

def paper_state() -> tuple:
    """Return (all_time_realized_pnl, reserved_in_open_trades, open_trade_count).
    Used on startup to restore _paper_balance and _daily_gate after a redeploy."""
    with _cur() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) AS pnl FROM trades "
            "WHERE paper=1 AND status LIKE 'closed_%%'"
        )
        realized = float(cur.fetchone()["pnl"])
        cur.execute(
            "SELECT COALESCE(SUM(risk_usd), 0) AS res, COUNT(*) AS cnt "
            "FROM trades WHERE paper=1 AND status='open'"
        )
        row = cur.fetchone()
        return realized, float(row["res"]), int(row["cnt"])


def get_pending_trades(paper: Optional[bool] = None) -> List[Trade]:
    with _cur() as cur:
        if paper is None:
            cur.execute("SELECT * FROM trades WHERE status='pending'")
        else:
            cur.execute(
                "SELECT * FROM trades WHERE status='pending' AND paper=%s", (int(paper),)
            )
        return [_row_to_trade(r) for r in cur.fetchall()]


def activate_trade(trade_id: int) -> Optional["Trade"]:
    """Flip a pending trade to open (entry filled)."""
    with _cur() as cur:
        cur.execute(
            "UPDATE trades SET status='open', pending_until=NULL, fill_time=%s "
            "WHERE id=%s AND status='pending'",
            (datetime.utcnow().isoformat(), trade_id),
        )
        cur.execute("SELECT * FROM trades WHERE id=%s", (trade_id,))
        row = cur.fetchone()
        return _row_to_trade(row) if row else None


def cancel_pending(trade_id: int) -> None:
    """Cancel a pending trade that expired or whose setup failed."""
    with _cur() as cur:
        cur.execute(
            "UPDATE trades SET status='cancelled' WHERE id=%s AND status='pending'",
            (trade_id,),
        )


# ── Signal attribution ────────────────────────────────────────────────────────

def log_signal(
    pair: str, direction: str, confidence: int, quality: str,
    session: str, regime: str, rr: float, entry: float, sl: float, tp: float,
    paper: bool, placed: bool = False, skip_reason: Optional[str] = None,
    atr: Optional[float] = None, price_at_gen: Optional[float] = None,
    stage: str = "queued", trade_type: Optional[str] = None,
    run_tag: Optional[str] = None,
) -> int:
    """Insert a signal_log row. Returns its id for later updates."""
    with _cur() as cur:
        cur.execute("""
            INSERT INTO signal_log
              (signal_time, pair, direction, confidence, quality, session, regime,
               rr, entry, sl, tp, paper, placed, skip_reason,
               atr, price_at_gen, stage, trade_type, run_tag)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            datetime.utcnow().isoformat(),
            pair, direction, confidence, quality, session, regime,
            float(rr), float(entry), float(sl), float(tp),
            int(paper), int(placed), skip_reason,
            float(atr) if atr is not None else None,
            float(price_at_gen) if price_at_gen is not None else None,
            stage,
            trade_type or "ICT_SWEEP_BREAKER",
            run_tag,
        ))
        return cur.fetchone()["id"]


def update_signal_skip(signal_id: int, reason: str) -> None:
    """Mark a placed=False signal with the reason it was rejected."""
    with _cur() as cur:
        cur.execute(
            "UPDATE signal_log SET skip_reason=%s, stage='cancelled' WHERE id=%s",
            (reason, signal_id),
        )


def link_signal_to_trade(signal_id: int, trade_id: int) -> None:
    """Mark signal as placed, link it to the trade row, and set stage='queued'."""
    with _cur() as cur:
        cur.execute(
            "UPDATE signal_log SET placed=1, trade_id=%s, stage='queued' WHERE id=%s",
            (trade_id, signal_id),
        )


def mark_signal_filled(trade_id: int) -> None:
    """Called when a pending order fills — flip filled=1 and stage='filled'."""
    with _cur() as cur:
        cur.execute(
            "UPDATE signal_log SET filled=1, stage='filled' WHERE trade_id=%s",
            (trade_id,),
        )


def mark_signal_unfilled(
    trade_id: int, direction: str, entry: float, current_price: Optional[float]
) -> None:
    """Called when a pending order expires without filling.

    Records filled=0 and, if we have a current price, whether the directional
    call was correct (price moved in the predicted direction from entry).
    """
    correct: Optional[int] = None
    if current_price is not None:
        if direction == "SELL":
            correct = 1 if current_price < entry else 0
        else:
            correct = 1 if current_price > entry else 0

    with _cur() as cur:
        cur.execute("""
            UPDATE signal_log
            SET filled=0, outcome='unfilled', stage='expired',
                direction_correct=%s, price_at_expiry=%s
            WHERE trade_id=%s
        """, (correct, current_price, trade_id))


def update_signal_outcome(trade_id: int, outcome: str, pnl_usd: float) -> None:
    """Called when a filled trade closes. outcome: 'tp' | 'sl' | 'time_stop'."""
    with _cur() as cur:
        cur.execute("""
            UPDATE signal_log SET outcome=%s, pnl_usd=%s
            WHERE trade_id=%s
        """, (outcome, round(pnl_usd, 4), trade_id))


# ── Signal analytics ──────────────────────────────────────────────────────────

_VALID_GROUP_COLS = {"session", "regime", "direction", "quality", "pair"}


def signal_overview(paper: bool) -> Dict:
    """High-level funnel counts across all signal_log rows."""
    with _cur() as cur:
        cur.execute("""
            SELECT
                COUNT(*)                                             AS total,
                SUM(CASE WHEN skip_reason='near_miss' THEN 1 ELSE 0 END) AS near_miss,
                SUM(placed)                                          AS placed,
                SUM(CASE WHEN filled=1 THEN 1 ELSE 0 END)           AS filled,
                SUM(CASE WHEN outcome='tp'  THEN 1 ELSE 0 END)      AS wins,
                SUM(CASE WHEN outcome='sl'  THEN 1 ELSE 0 END)      AS losses,
                SUM(CASE WHEN outcome='time_stop' THEN 1 ELSE 0 END) AS time_stops,
                SUM(CASE WHEN outcome='unfilled'  THEN 1 ELSE 0 END) AS unfilled,
                SUM(CASE WHEN direction_correct=1 THEN 1 ELSE 0 END) AS dir_correct,
                SUM(CASE WHEN direction_correct IS NOT NULL THEN 1 ELSE 0 END) AS dir_checked,
                ROUND(CAST(SUM(COALESCE(pnl_usd,0)) AS NUMERIC),2)  AS total_pnl,
                MIN(signal_time)                                     AS first_signal
            FROM signal_log
            WHERE paper=%s
        """, (int(paper),))
        return dict(cur.fetchone())


def signal_stats_by_group(group_col: str, paper: bool) -> List[Dict]:
    """
    Returns per-bucket stats grouped by the given column.
    group_col: session | regime | direction | quality | pair | confidence_band
    """
    if group_col == "confidence_band":
        with _cur() as cur:
            cur.execute("""
                SELECT
                    ((confidence / 10) * 10)::TEXT || '-' ||
                    ((confidence / 10) * 10 + 9)::TEXT          AS group_key,
                    COUNT(*)                                      AS total,
                    SUM(placed)                                   AS placed,
                    SUM(CASE WHEN filled=1  THEN 1 ELSE 0 END)   AS filled,
                    SUM(CASE WHEN outcome='tp' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN outcome='sl' THEN 1 ELSE 0 END) AS losses,
                    ROUND(CAST(AVG(CASE WHEN filled=1 THEN pnl_usd END) AS NUMERIC),2) AS avg_pnl,
                    ROUND(CAST(AVG(rr) AS NUMERIC),1)             AS avg_rr
                FROM signal_log
                WHERE paper=%s AND skip_reason IS DISTINCT FROM 'near_miss'
                GROUP BY (confidence / 10) * 10
                ORDER BY (confidence / 10) * 10
            """, (int(paper),))
            return [dict(r) for r in cur.fetchall()]

    if group_col not in _VALID_GROUP_COLS:
        return []

    with _cur() as cur:
        cur.execute(f"""
            SELECT
                {group_col}                                           AS group_key,
                COUNT(*)                                              AS total,
                SUM(placed)                                           AS placed,
                SUM(CASE WHEN filled=1  THEN 1 ELSE 0 END)           AS filled,
                SUM(CASE WHEN outcome='tp' THEN 1 ELSE 0 END)         AS wins,
                SUM(CASE WHEN outcome='sl' THEN 1 ELSE 0 END)         AS losses,
                ROUND(CAST(AVG(CASE WHEN filled=1 THEN pnl_usd END) AS NUMERIC),2) AS avg_pnl,
                ROUND(CAST(AVG(rr) AS NUMERIC),1)                     AS avg_rr
            FROM signal_log
            WHERE paper=%s AND skip_reason IS DISTINCT FROM 'near_miss'
            GROUP BY {group_col}
            ORDER BY SUM(COALESCE(pnl_usd,0)) DESC
        """, (int(paper),))
        return [dict(r) for r in cur.fetchall()]


def edge_funnel(paper: bool, days: int = 30, run_tag: Optional[str] = None) -> Dict:
    """Stage-based signal funnel counts for the last N days."""
    tag_sql = "AND run_tag = %s" if run_tag else ""
    params = [int(paper)] + ([run_tag] if run_tag else [])
    with _cur() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*)                                                              AS total,
                SUM(CASE WHEN stage IN ('queued','filled','expired') THEN 1 ELSE 0 END) AS went_to_queue,
                SUM(CASE WHEN stage = 'filled'   THEN 1 ELSE 0 END)                  AS filled,
                SUM(CASE WHEN stage = 'expired'  THEN 1 ELSE 0 END)                  AS expired,
                SUM(CASE WHEN stage = 'cancelled' THEN 1 ELSE 0 END)                 AS cancelled,
                SUM(CASE WHEN skip_reason = 'near_miss' THEN 1 ELSE 0 END)           AS near_miss,
                -- legacy execution metrics (filled trades)
                SUM(CASE WHEN outcome = 'tp'        THEN 1 ELSE 0 END)               AS wins,
                SUM(CASE WHEN outcome = 'sl'        THEN 1 ELSE 0 END)               AS losses,
                SUM(CASE WHEN outcome = 'time_stop' THEN 1 ELSE 0 END)               AS time_stops,
                ROUND(CAST(SUM(COALESCE(pnl_usd,0)) AS NUMERIC), 2)                  AS total_pnl,
                MIN(signal_time)                                                      AS first_signal
            FROM signal_log
            WHERE paper = %s
              {tag_sql}
              AND skip_reason IS DISTINCT FROM 'near_miss'
              AND signal_time::TIMESTAMP >= NOW() - INTERVAL '{days} days'
        """, tuple(params))
        return dict(cur.fetchone())


def edge_direction_stats(paper: bool, days: int = 30, group_by: Optional[str] = None,
                         run_tag: Optional[str] = None) -> Dict:
    """
    Direction accuracy at 4h and 24h, plus win-rate/P&L of filled trades.
    group_by: None (overall) | 'session' | 'regime' | 'direction' |
              'confidence_band' | 'pair' | 'quality' | 'trade_type'
    """
    _valid = {"session", "regime", "direction", "pair", "quality", "trade_type"}
    tag_sql = "AND run_tag = %s" if run_tag else ""
    params = [int(paper)] + ([run_tag] if run_tag else [])

    if group_by is None:
        with _cur() as cur:
            cur.execute(f"""
                SELECT
                    -- direction accuracy
                    COUNT(*) FILTER (WHERE dir_4h IS NOT NULL)                         AS n_4h,
                    SUM(CASE WHEN dir_4h  = TRUE THEN 1 ELSE 0 END)                    AS ok_4h,
                    COUNT(*) FILTER (WHERE dir_4h IS NOT NULL AND direction='BUY')     AS n_4h_buy,
                    SUM(CASE WHEN dir_4h=TRUE  AND direction='BUY'  THEN 1 ELSE 0 END) AS ok_4h_buy,
                    COUNT(*) FILTER (WHERE dir_4h IS NOT NULL AND direction='SELL')    AS n_4h_sell,
                    SUM(CASE WHEN dir_4h=TRUE  AND direction='SELL' THEN 1 ELSE 0 END) AS ok_4h_sell,
                    COUNT(*) FILTER (WHERE dir_24h IS NOT NULL)                        AS n_24h,
                    SUM(CASE WHEN dir_24h = TRUE THEN 1 ELSE 0 END)                    AS ok_24h,
                    COUNT(*) FILTER (WHERE dir_24h IS NOT NULL AND direction='BUY')    AS n_24h_buy,
                    SUM(CASE WHEN dir_24h=TRUE AND direction='BUY'  THEN 1 ELSE 0 END) AS ok_24h_buy,
                    COUNT(*) FILTER (WHERE dir_24h IS NOT NULL AND direction='SELL')   AS n_24h_sell,
                    SUM(CASE WHEN dir_24h=TRUE AND direction='SELL' THEN 1 ELSE 0 END) AS ok_24h_sell,
                    -- execution (filled trades only)
                    SUM(CASE WHEN filled=1 THEN 1 ELSE 0 END)                          AS n_filled,
                    SUM(CASE WHEN outcome='tp'  THEN 1 ELSE 0 END)                     AS wins,
                    SUM(CASE WHEN outcome='sl'  THEN 1 ELSE 0 END)                     AS losses,
                    SUM(CASE WHEN outcome='time_stop' THEN 1 ELSE 0 END)               AS time_stops,
                    ROUND(CAST(AVG(CASE WHEN filled=1 THEN pnl_usd END) AS NUMERIC),2) AS avg_pnl,
                    ROUND(CAST(AVG(rr) AS NUMERIC),1)                                  AS avg_rr
                FROM signal_log
                WHERE paper = %s
                  {tag_sql}
                  AND skip_reason IS DISTINCT FROM 'near_miss'
                  AND signal_time::TIMESTAMP >= NOW() - INTERVAL '{days} days'
            """, tuple(params))
            return {"overall": dict(cur.fetchone())}

    if group_by == "confidence_band":
        group_expr = (
            "CASE WHEN confidence >= 80 THEN '80-100' "
            "     WHEN confidence >= 65 THEN '65-79' "
            "     ELSE '55-64' END"
        )
        order_expr = "MIN(confidence) DESC"
    elif group_by in _valid:
        group_expr = group_by
        order_expr = "SUM(COALESCE(pnl_usd,0)) DESC"
    else:
        return {}

    with _cur() as cur:
        cur.execute(f"""
            SELECT
                {group_expr}                                                           AS group_key,
                COUNT(*)                                                               AS total,
                SUM(CASE WHEN stage IN ('queued','filled','expired') THEN 1 ELSE 0 END) AS went_to_queue,
                SUM(CASE WHEN stage = 'filled' THEN 1 ELSE 0 END)                     AS filled,
                -- direction accuracy
                COUNT(*) FILTER (WHERE dir_4h IS NOT NULL)                             AS n_4h,
                SUM(CASE WHEN dir_4h = TRUE THEN 1 ELSE 0 END)                         AS ok_4h,
                COUNT(*) FILTER (WHERE dir_24h IS NOT NULL)                            AS n_24h,
                SUM(CASE WHEN dir_24h = TRUE THEN 1 ELSE 0 END)                        AS ok_24h,
                -- execution
                SUM(CASE WHEN outcome='tp'  THEN 1 ELSE 0 END)                         AS wins,
                SUM(CASE WHEN outcome='sl'  THEN 1 ELSE 0 END)                         AS losses,
                SUM(CASE WHEN outcome='time_stop' THEN 1 ELSE 0 END)                   AS time_stops,
                ROUND(CAST(AVG(CASE WHEN filled=1 THEN pnl_usd END) AS NUMERIC),2)     AS avg_pnl,
                ROUND(CAST(AVG(rr) AS NUMERIC),1)                                      AS avg_rr
            FROM signal_log
            WHERE paper = %s
              {tag_sql}
              AND skip_reason IS DISTINCT FROM 'near_miss'
              AND signal_time::TIMESTAMP >= NOW() - INTERVAL '{days} days'
            GROUP BY {group_expr}
            ORDER BY {order_expr}
        """, tuple(params))
        return {"rows": [dict(r) for r in cur.fetchall()]}


def edge_atr_buckets(paper: bool, days: int = 30, run_tag: Optional[str] = None) -> List[Dict]:
    """Direction accuracy split by ATR% environment (<2%, 2-5%, >5%)."""
    tag_sql = "AND run_tag = %s" if run_tag else ""
    params = [int(paper)] + ([run_tag] if run_tag else [])
    with _cur() as cur:
        cur.execute(f"""
            SELECT
                CASE
                    WHEN atr / NULLIF(price_at_gen, 0) * 100 < 2 THEN '<2%%'
                    WHEN atr / NULLIF(price_at_gen, 0) * 100 < 5 THEN '2-5%%'
                    ELSE '>5%%'
                END                                                                    AS bucket,
                COUNT(*)                                                               AS total,
                COUNT(*) FILTER (WHERE dir_4h IS NOT NULL)                             AS n_4h,
                SUM(CASE WHEN dir_4h  = TRUE THEN 1 ELSE 0 END)                        AS ok_4h,
                COUNT(*) FILTER (WHERE dir_24h IS NOT NULL)                            AS n_24h,
                SUM(CASE WHEN dir_24h = TRUE THEN 1 ELSE 0 END)                        AS ok_24h,
                SUM(CASE WHEN outcome='tp' THEN 1 ELSE 0 END)                          AS wins,
                SUM(CASE WHEN outcome='sl' THEN 1 ELSE 0 END)                          AS losses,
                ROUND(CAST(AVG(CASE WHEN filled=1 THEN pnl_usd END) AS NUMERIC),2)     AS avg_pnl
            FROM signal_log
            WHERE paper = %s
              {tag_sql}
              AND atr IS NOT NULL AND price_at_gen IS NOT NULL
              AND skip_reason IS DISTINCT FROM 'near_miss'
              AND signal_time::TIMESTAMP >= NOW() - INTERVAL '{days} days'
            GROUP BY bucket
            ORDER BY MIN(atr / NULLIF(price_at_gen, 0) * 100)
        """, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def edge_setup_quality(paper: bool, days: int = 30, run_tag: Optional[str] = None) -> Dict:
    """Outcome stats for expired (unfilled) signals with computed setup_outcome."""
    tag_sql = "AND run_tag = %s" if run_tag else ""
    params = [int(paper)] + ([run_tag] if run_tag else [])
    with _cur() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE setup_outcome IS NOT NULL)                      AS n_resolved,
                COUNT(*) FILTER (WHERE entry_reached = TRUE)                           AS n_entry_hit,
                COUNT(*) FILTER (WHERE tp1_reached = TRUE AND entry_reached = TRUE
                                   AND NOT COALESCE(sl_reached, FALSE))               AS n_wins,
                COUNT(*) FILTER (WHERE sl_reached  = TRUE AND entry_reached = TRUE
                                   AND NOT COALESCE(tp1_reached, FALSE))              AS n_losses,
                COUNT(*) FILTER (WHERE setup_outcome = 'no_entry')                     AS n_no_entry,
                COUNT(*) FILTER (WHERE setup_outcome = 'ambiguous')                    AS n_ambiguous,
                COUNT(*) FILTER (WHERE setup_outcome IS NULL
                                   AND signal_time::TIMESTAMP <= NOW() - INTERVAL '72 hours') AS n_pending_compute
            FROM signal_log
            WHERE paper = %s
              {tag_sql}
              AND stage = 'expired'
              AND signal_time::TIMESTAMP >= NOW() - INTERVAL '{days} days'
        """, tuple(params))
        return dict(cur.fetchone())


def edge_pairs_ranked(paper: bool, days: int = 30, run_tag: Optional[str] = None) -> List[Dict]:
    """Pairs ranked by fill rate, direction accuracy, and setup win rate."""
    tag_sql = "AND run_tag = %s" if run_tag else ""
    params = [int(paper)] + ([run_tag] if run_tag else [])
    with _cur() as cur:
        cur.execute(f"""
            SELECT
                pair,
                COUNT(*)                                                               AS total,
                SUM(CASE WHEN stage IN ('queued','filled','expired') THEN 1 ELSE 0 END) AS went_to_queue,
                SUM(CASE WHEN stage = 'filled' THEN 1 ELSE 0 END)                     AS filled,
                COUNT(*) FILTER (WHERE dir_4h IS NOT NULL)                             AS n_dir,
                SUM(CASE WHEN dir_4h = TRUE THEN 1 ELSE 0 END)                         AS ok_dir,
                SUM(CASE WHEN outcome = 'tp' THEN 1 ELSE 0 END)                        AS wins,
                SUM(CASE WHEN outcome = 'sl' THEN 1 ELSE 0 END)                        AS losses,
                ROUND(CAST(SUM(COALESCE(pnl_usd,0)) AS NUMERIC),2)                    AS total_pnl,
                COUNT(*) FILTER (WHERE setup_outcome IS NOT NULL AND entry_reached)    AS n_setup,
                SUM(CASE WHEN setup_outcome = 'win' THEN 1 ELSE 0 END)                 AS n_setup_win
            FROM signal_log
            WHERE paper = %s
              {tag_sql}
              AND skip_reason IS DISTINCT FROM 'near_miss'
              AND signal_time::TIMESTAMP >= NOW() - INTERVAL '{days} days'
            GROUP BY pair
            ORDER BY SUM(COALESCE(pnl_usd,0)) DESC
        """, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def diagnose_execution_stats(paper: bool, run_tag: Optional[str] = None) -> Dict:
    """Execution layer stats: win rate, expectancy, BE TP1 tracking."""
    tag_sql = "AND run_tag = %s" if run_tag else ""
    params = [int(paper)] + ([run_tag] if run_tag else [])
    with _cur() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*)                                                               AS total,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)                          AS wins,
                SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END)                         AS losses,
                -- BE wins: closed at SL but SL was moved to entry (pnl ≈ 0)
                SUM(CASE WHEN status='closed_sl'
                          AND ABS(close_price - entry) / NULLIF(entry,0) < 0.005
                          THEN 1 ELSE 0 END)                                           AS be_wins,
                ROUND(CAST(AVG(pnl_usd) AS NUMERIC),4)                                AS avg_pnl,
                ROUND(CAST(SUM(pnl_usd) AS NUMERIC),2)                                AS total_pnl,
                -- Post-BE TP1 tracking
                COUNT(*) FILTER (WHERE post_be_tp1 IS NOT NULL)                        AS be_tp1_checked,
                SUM(CASE WHEN post_be_tp1 = TRUE THEN 1 ELSE 0 END)                    AS be_tp1_hit
            FROM trades
            WHERE paper = %s
              {tag_sql}
              AND status LIKE 'closed_%%'
        """, tuple(params))
        return dict(cur.fetchone())


def move_sl_to_breakeven(trade_id: int) -> bool:
    """Move SL to entry price for an open trade. Returns True if the row was updated."""
    with _cur() as cur:
        cur.execute(
            "UPDATE trades SET sl = entry WHERE id = %s AND status = 'open' AND sl != entry",
            (trade_id,),
        )
        return cur.rowcount > 0


def get_open_trades(paper: Optional[bool] = None) -> List[Trade]:
    with _cur() as cur:
        if paper is None:
            cur.execute("SELECT * FROM trades WHERE status='open'")
        else:
            cur.execute(
                "SELECT * FROM trades WHERE status='open' AND paper=%s", (int(paper),)
            )
        return [_row_to_trade(r) for r in cur.fetchall()]


def get_all_trades(paper: Optional[bool] = None) -> List[Trade]:
    with _cur() as cur:
        if paper is None:
            cur.execute("SELECT * FROM trades ORDER BY open_time")
        else:
            cur.execute(
                "SELECT * FROM trades WHERE paper=%s ORDER BY open_time", (int(paper),)
            )
        return [_row_to_trade(r) for r in cur.fetchall()]


def get_recent_trades(limit: int = 10, paper: Optional[bool] = None) -> List[Trade]:
    limit = max(1, min(limit, 50))
    with _cur() as cur:
        if paper is None:
            cur.execute(
                "SELECT * FROM trades WHERE status LIKE 'closed_%%' "
                "ORDER BY close_time DESC LIMIT %s",
                (limit,),
            )
        else:
            cur.execute(
                "SELECT * FROM trades WHERE status LIKE 'closed_%%' AND paper=%s "
                "ORDER BY close_time DESC LIMIT %s",
                (int(paper), limit),
            )
        return [_row_to_trade(r) for r in cur.fetchall()]


def all_time_summary(paper: Optional[bool] = None, run_tag: Optional[str] = None) -> Dict:
    conditions = ["status LIKE 'closed_%%'"]
    params: list = []
    if paper is not None:
        conditions.append("paper = %s")
        params.append(int(paper))
    if run_tag is not None:
        conditions.append("run_tag = %s")
        params.append(run_tag)
    with _cur() as cur:
        cur.execute(f"SELECT * FROM trades WHERE {' AND '.join(conditions)}", params)
        rows = cur.fetchall()

    trades    = [_row_to_trade(r) for r in rows]
    if not trades:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0.0,
            "avg_rr": 0, "avg_mae_usd": 0.0, "avg_mfe_usd": 0.0,
            "avg_mae_time_sec": 0, "avg_mfe_time_sec": 0,
            "mfe_capture_pct": None, "best_trade": None, "worst_trade": None, "since": None,
        }

    wins      = [t for t in trades if (t.pnl_usd or 0) > 0]
    losses    = [t for t in trades if (t.pnl_usd or 0) <= 0]
    total_pnl = sum(t.pnl_usd or 0 for t in trades)
    n         = len(trades)

    avg_mae_usd      = round(sum(t.mae_usd or 0 for t in trades) / n, 2)
    avg_mfe_usd      = round(sum(t.mfe_usd or 0 for t in trades) / n, 2)
    avg_mae_time_sec = round(sum(t.time_to_mae_sec or 0 for t in trades) / n)
    avg_mfe_time_sec = round(sum(t.time_to_mfe_sec or 0 for t in trades) / n)

    cap_trades = [t for t in trades if t.mfe_usd and t.mfe_usd > 0 and (t.pnl_usd or 0) > 0]
    mfe_capture_pct = (
        round(sum((t.pnl_usd or 0) / t.mfe_usd for t in cap_trades) / len(cap_trades) * 100, 1)
        if cap_trades else None
    )

    best  = max(trades, key=lambda t: t.pnl_usd or 0)
    worst = min(trades, key=lambda t: t.pnl_usd or 0)
    since = min(t.open_time for t in trades).date()

    return {
        "total":            n,
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         round(len(wins) / n, 2),
        "total_pnl":        round(total_pnl, 2),
        "avg_rr":           round(sum(t.rr for t in trades) / n, 2),
        "avg_mae_usd":      avg_mae_usd,
        "avg_mfe_usd":      avg_mfe_usd,
        "avg_mae_time_sec": avg_mae_time_sec,
        "avg_mfe_time_sec": avg_mfe_time_sec,
        "mfe_capture_pct":  mfe_capture_pct,
        "best_trade":       {"pair": best.pair,  "pnl": round(best.pnl_usd or 0, 2)},
        "worst_trade":      {"pair": worst.pair, "pnl": round(worst.pnl_usd or 0, 2)},
        "since":            str(since),
    }


def daily_summary(target_date: date = None) -> Dict:
    target_date = target_date or date.today()
    prefix = target_date.isoformat()

    with _cur() as cur:
        cur.execute("""
            SELECT * FROM trades
            WHERE close_time LIKE %s AND status LIKE 'closed_%%'
        """, (f"{prefix}%",))
        rows = cur.fetchall()

    trades    = [_row_to_trade(r) for r in rows]
    wins      = [t for t in trades if (t.pnl_usd or 0) > 0]
    losses    = [t for t in trades if (t.pnl_usd or 0) <= 0]
    total_pnl = sum(t.pnl_usd or 0 for t in trades)
    n         = len(trades) or 1

    avg_mae_usd      = round(sum(t.mae_usd or 0 for t in trades) / n, 2)
    avg_mfe_usd      = round(sum(t.mfe_usd or 0 for t in trades) / n, 2)
    avg_mae_time_sec = round(sum(t.time_to_mae_sec or 0 for t in trades) / n)
    avg_mfe_time_sec = round(sum(t.time_to_mfe_sec or 0 for t in trades) / n)

    cap_trades = [t for t in trades if t.mfe_usd and t.mfe_usd > 0 and (t.pnl_usd or 0) > 0]
    mfe_capture_pct = (
        round(sum((t.pnl_usd or 0) / t.mfe_usd for t in cap_trades) / len(cap_trades) * 100, 1)
        if cap_trades else None
    )

    return {
        "date":             str(target_date),
        "total":            len(trades),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         round(len(wins) / len(trades), 2) if trades else 0,
        "total_pnl":        round(total_pnl, 2),
        "avg_rr":           round(sum(t.rr for t in trades) / len(trades), 2) if trades else 0,
        "avg_mae_usd":      avg_mae_usd,
        "avg_mfe_usd":      avg_mfe_usd,
        "avg_mae_time_sec": avg_mae_time_sec,
        "avg_mfe_time_sec": avg_mfe_time_sec,
        "mfe_capture_pct":  mfe_capture_pct,
    }


# ── Report formatting ─────────────────────────────────────────────────────────

def _fmt_sec(sec: int) -> str:
    if sec < 60:   return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m"
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def format_daily_report(summary: Dict) -> str:
    wr   = summary["win_rate"] * 100
    pnl  = summary["total_pnl"]
    icon = "🟢" if pnl >= 0 else "🔴"

    mae_line = (
        f"MAE avg: ${summary['avg_mae_usd']:.2f} "
        f"({_fmt_sec(summary['avg_mae_time_sec'])} to worst)  |  "
        f"MFE avg: ${summary['avg_mfe_usd']:.2f} "
        f"({_fmt_sec(summary['avg_mfe_time_sec'])} to peak)"
    )
    cap     = summary.get("mfe_capture_pct")
    cap_line = f"MFE captured: {cap:.1f}%" if cap is not None else "MFE captured: —"

    return (
        f"{icon} Daily Report — {summary['date']}\n"
        f"Trades: {summary['total']} | W:{summary['wins']} L:{summary['losses']} | WR:{wr:.0f}%\n"
        f"P&L: ${pnl:+.2f} | Avg RR: {summary['avg_rr']}\n"
        f"\n"
        f"{mae_line}\n"
        f"{cap_line}\n"
    )


# ── Internal ──────────────────────────────────────────────────────────────────

def _row_to_trade(row) -> Trade:
    r = dict(row)
    return Trade(
        id               = r["id"],
        pair             = r["pair"],
        direction        = r["direction"],
        entry            = r["entry"],
        sl               = r["sl"],
        tp               = r["tp"],
        rr               = r["rr"],
        qty              = r["qty"],
        risk_usd         = r["risk_usd"],
        strategy         = r["strategy"],
        confidence       = r["confidence"],
        status           = r["status"],
        open_time        = datetime.fromisoformat(r["open_time"]),
        close_time       = datetime.fromisoformat(r["close_time"]) if r["close_time"] else None,
        close_price      = r["close_price"],
        pnl_usd          = r["pnl_usd"],
        paper            = bool(r["paper"]),
        mae_usd          = r.get("mae_usd"),
        mfe_usd          = r.get("mfe_usd"),
        time_to_mae_sec  = r.get("time_to_mae_sec"),
        time_to_mfe_sec  = r.get("time_to_mfe_sec"),
        pending_until    = (datetime.fromisoformat(r["pending_until"])
                            if r.get("pending_until") else None),
        fill_time        = (datetime.fromisoformat(r["fill_time"])
                            if r.get("fill_time") else None),
        run_tag          = r.get("run_tag"),
    )
