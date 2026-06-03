#!/usr/bin/env python3
"""
buffer_status.py — read-only readout of how full each /diagnose & /edge buffer is.

For every analytics layer it prints the current count vs its N-gate threshold, and
whether the buffer is fed by SIGNALS (accumulates from every signal, regardless of
fills or MAX_OPEN_TRADES) or by FILLS (needs executed, closed trades). No writes —
safe to run anytime.

Run where DATABASE_URL is set, e.g. on Railway:
    railway run python buffer_status.py
"""
from __future__ import annotations
import os

import config
import performance as perf
import analytics as an


def _bar(cur: int, need: int) -> str:
    return "✓ ready" if cur >= need else f"⏳ {cur}/{need}"


def main() -> None:
    if not os.getenv("DATABASE_URL"):
        print(
            "DATABASE_URL not set — run on Railway (`railway run python buffer_status.py`) "
            "or export the DB URL first."
        )
        return

    paper  = config.PAPER_TRADING
    funnel = perf.edge_funnel(paper, 30)
    dirs   = perf.edge_direction_stats(paper, 30)["overall"]
    setup  = perf.edge_setup_quality(paper, 30)
    exec_  = perf.diagnose_execution_stats(paper)

    total   = int(funnel["total"] or 0)
    queued  = int(funnel["went_to_queue"] or 0)
    filled  = int(funnel["filled"] or 0)
    expired = int(funnel["expired"] or 0)

    n4   = int(dirs["n_4h"]      or 0)
    n4b  = int(dirs["n_4h_buy"]  or 0)
    n4s  = int(dirs["n_4h_sell"] or 0)
    n24  = int(dirs["n_24h"]     or 0)

    n_res  = int(setup["n_resolved"]        or 0)
    n_hit  = int(setup["n_entry_hit"]       or 0)
    n_pend = int(setup["n_pending_compute"] or 0)

    n_exec = int(exec_["total"]   or 0)
    n_be   = int(exec_["be_wins"] or 0)

    print(f"\n  CB-Bot buffer status — {'paper' if paper else 'live'} · last 30d")
    print(f"  Signals logged: {total}  (queued {queued} · filled {filled} · expired {expired})\n")

    print("  SIGNAL-FED — accumulate from every signal; MAX_OPEN_TRADES is irrelevant")
    print(f"   L2 Direction @4h  overall   {_bar(n4,  an._N_DIR_TOTAL)}     (~4h lag/signal)")
    print(f"   L2 Direction @4h  BUY       {_bar(n4b, an._N_DIR_SIDE)}")
    print(f"   L2 Direction @4h  SELL      {_bar(n4s, an._N_DIR_SIDE)}")
    print(f"   L2 Direction @24h overall   {_bar(n24, an._N_DIR_TOTAL)}     (~24h lag/signal)")
    print(f"   L3 Setup entry-hit rate     {_bar(n_res, an._N_SETUP)}     ({n_pend} awaiting 72h compute)")
    print(f"   L3 Setup win-when-hit       {_bar(n_hit, an._N_ENTRY_HIT)}")
    print()
    print("  FILL-FED — need executed, closed trades (this is what trade volume gates)")
    print(f"   L4 Execution win rate / EV  {_bar(n_exec, an._N_EXEC)}")
    print(f"   L4 Post-BE TP1 timing       {_bar(n_be,   an._N_BE)}")
    print()


if __name__ == "__main__":
    main()
