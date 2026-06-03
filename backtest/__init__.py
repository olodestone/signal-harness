"""Backtest package — historical replay that seeds the harness DB so the EXISTING
/edge + /diagnose analytics can compare strategies over the last 30 days.

Nothing here touches the production harness files (strategy.py / bot.py / analytics.py
/ performance.py / exchange.py / config.py). It reuses their functions read-only and
mirrors the single signal INSERT it needs with a historical timestamp.
"""
