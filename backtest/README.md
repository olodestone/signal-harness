# backtest — 30-day strategy edge comparison

A historical **replay** that seeds the harness PostgreSQL DB with back-dated signals +
resolved 72h outcomes, so the **existing** `/edge` and `/diagnose` analytics
(`analytics.py`) can compare strategies over the last 30 days *without* waiting for the
bot to run live.

It does **not** modify any production harness file. `strategy.py` stays a stub. The four
candidate strategies live in `backtest/strategies.py` and are scored independently via
per-strategy `run_tag`.

## Why this exists

The harness is forward-only: `/edge`/`/diagnose` read `signal_log` filtered to
`NOW() - 30 days` and only resolve a signal's setup once it's `>72h` old. To get an answer
now, the replay walks 30 days of KuCoin OHLCV bar-by-bar (no lookahead), runs each strategy
at every 15m close, logs the signals with their historical timestamps, then resolves
outcomes + 4h/24h direction snapshots from the same pre-fetched data.

## Strategies (run_tags)

| run_tag | family |
|---------|--------|
| `bt_sr`    | Support/Resistance rejection |
| `bt_bos`   | Market-structure break + retest |
| `bt_trend` | Trend pullback (EMA + candle) |
| `bt_sweep` | Liquidity sweep + breaker (ICT) |

## Run it

```bash
# 1. throwaway Postgres (Docker)
docker run -d --name signal-bt-pg -e POSTGRES_PASSWORD=bt -e POSTGRES_DB=bt \
  -p 5433:5432 postgres:16
export DATABASE_URL=postgresql://postgres:bt@localhost:5433/bt

# 2. seed the DB (default: live watchlist, all 4 strategies, 30 days)
python -m backtest.run_backtest --days 30

# 3. print /edge + /diagnose per strategy + the ranking
python -m backtest.report --days 30

# teardown
docker rm -f signal-bt-pg
```

Smoke test first:
`python -m backtest.run_backtest --days 8 --pairs BTC/USDT,ETH/USDT --strategies bt_sr`

## Ranking

The harness has no execution, so Layer-1 (fill) and Layer-4 (P&L) read empty by design.
Strategies are ranked on what it measures: Layer-3 **win-when-hit %** (gated by ≥10 resolved
and ≥30% entry-hit), then Layer-2 **direction accuracy** (dir@4h, dir@24h), then sample size.
