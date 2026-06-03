# signal-harness

A **signal-quality harness** extracted from cb-bot. It gives you the *exact*
`/edge` and `/diagnose` analytics and the *exact* coin-fetching / universe logic,
with **no trading strategy** — you plug your own into one stub file.

There is **no execution**: the harness never places orders or sizes positions.
It exists to answer one question — *does my strategy generate good signals?* —
by logging every signal and replaying the next 72h of price to score it.

---

## What's included vs left out

| Included (works out of the box) | Left out (by design) |
|---|---|
| Coin universe + fetching (`exchange.py`) | Trading strategy (`strategy.py` is a stub) |
| PostgreSQL persistence (`performance.py`) | Technical indicators (bring your own) |
| `/edge` + `/diagnose` analytics (`analytics.py`) | Backtester |
| Telegram command server (`telegram_commands.py`) | Order placement / fills |
| Notifications (`notify.py`) | Risk sizing / daily loss gate |
| Slim scan loop (`bot.py`) | |

---

## How coins are fetched (`exchange.py` + `config.py`)

Data comes from the **public KuCoin REST API** — no API key needed.

The scanned universe is a **hybrid watchlist** = a curated core **plus** dynamic
additions:

- **Core** (`WATCHLIST`, 20 majors) is *always* scanned, regardless of its
  (often thin) KuCoin spot volume.
- **Dynamic** additions fill up to `MAX_PAIRS` with pairs that clear every gate:
  - 24h volume ≥ `MIN_VOLUME_USDT` (default $10M)
  - ≥ `MIN_PAIR_AGE_DAYS` of price history (default 300d → excludes fresh pump
    listings)
  - not in `PAIR_BLACKLIST`
- The watchlist is cached for 4h; OHLCV is cached per-timeframe (15m→60s, 1h→10m,
  4h→1h, 1d→12h) and the cache is cleared at the start of each scan.

Each pair is fetched on four timeframes (`ENTRY_TF`/`CONFIRM_TF`/`TREND_TF`/
`BIAS_TF`, default 15m/1h/4h/1d) and handed to your `generate_signal`.

---

## Plugging in your strategy

Implement one function in **`strategy.py`**:

```python
def generate_signal(df_entry, df_confirm, df_trend, df_bias, bar_time=None) -> Optional[Signal]:
    # your detection + scoring
    return Signal(direction="BUY", entry=..., current_price=..., sl=..., tp=...,
                  rr=..., strategy="my_v1", confidence=82, quality="high",
                  regime="trending_up", session=session_for(bar_time), atr=...)
```

Return a `Signal` for the best setup or `None`. The harness handles fetching,
dedup, logging, snapshots, and all analytics. Do **not** add execution here.

---

## What `/edge` and `/diagnose` show (and when)

Because there's no execution, every signal is logged as `stage='expired'` and
evaluated by a **72h price replay** (`analytics.populate_outcomes`). So:

| Layer | Populates? | Notes |
|---|---|---|
| **Layer 2 — Direction accuracy** | ✅ yes | `dir_4h`/`dir_24h` captured live each scan |
| **Layer 3 — Setup quality** | ✅ yes | entry-hit rate + win-when-hit, from the 72h replay (signals must be **>72h old** first) |
| Layer 1 — Fill rate | ⚠️ reads 0% | no orders are placed — expected, ignore |
| Layer 4 — Execution | ⚠️ "need closed trades" | activates only if you add order placement |

`/edge` and `/diagnose` default to the current `RUN_TAG`; append `all` to pool
every run or `vN` for a specific one (e.g. `/edge dir all`, `/diagnose v1`).
Bump `RUN_TAG` whenever you change your strategy so the old and new signal sets
stay separable.

---

## Running

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in DATABASE_URL (+ TG_TOKEN/TG_CHAT_ID for Telegram)
python bot.py
```

Out of the box the stub returns no signals, so each scan logs `0 signal(s)`.
Once you implement `generate_signal`, signals accumulate and — after ~72h —
`/diagnose` Layers 2 and 3 tell you whether the strategy's direction calls and
setups actually resolve in your favour.

**Deployment:** Railway worker (`railway.toml`, `python bot.py`) with the
PostgreSQL addon. Heroku-style `Procfile` is also included.

---

## File map

```
bot.py                main scan loop (slim; no execution)
strategy.py           ← YOUR plug-in (stub returns None)
exchange.py           coin universe + KuCoin OHLCV/price fetching
performance.py        PostgreSQL: trades + signal_log schema & queries
analytics.py          /edge + /diagnose builders + replay workers
telegram_commands.py  Telegram command server (/edge, /diagnose, /stats, …)
notify.py             Telegram message formatting
config.py             all env-driven configuration
buffer_status.py      quick setup-quality readout
fill_time_report.py   fill-latency report (only meaningful once you add execution)
```
