"""
Fill-latency vs outcome report.

For every closed, filled trade it buckets time-to-fill (fill_time - open_time)
and shows win rate, avg P&L, and total P&L per bucket. This is the empirical
basis for FILL_TIMEOUT_SEC: the old 30-min cap rested on a 4-trade claim that
"fills >15 min never win" — this report tests that with the full sample.

Usage:
    railway run python fill_time_report.py            # paper (default)
    railway run python fill_time_report.py --live     # live trades
    DATABASE_URL=postgres://... python fill_time_report.py
"""
import argparse

import performance as perf

# (label, lower_bound_sec_inclusive, upper_bound_sec_exclusive | None)
BUCKETS = [
    ("0-5 min",    0,    300),
    ("5-11 min",   300,  660),
    ("11-15 min",  660,  900),
    ("15-30 min",  900,  1800),
    ("30-60 min",  1800, 3600),
    ("1-2 h",      3600, 7200),
    ("2 h+",       7200, None),
]


def main(paper: bool) -> None:
    with perf._cur() as cur:
        cur.execute(
            """
            SELECT
                EXTRACT(EPOCH FROM (fill_time::TIMESTAMP - open_time::TIMESTAMP)) AS fill_sec,
                pnl_usd
            FROM trades
            WHERE paper = %s
              AND fill_time IS NOT NULL
              AND status LIKE 'closed_%%'
            """,
            (int(paper),),
        )
        rows = [dict(r) for r in cur.fetchall()]

    mode = "paper" if paper else "live"
    if not rows:
        print(f"No closed, filled {mode} trades yet.")
        return

    valid = [r for r in rows if r["fill_sec"] is not None and r["fill_sec"] >= 0]
    print(f"\nFill-latency vs outcome — {mode} ({len(valid)} closed fills)\n")
    print(f"{'bucket':<12}{'n':>4}{'wins':>6}{'win%':>7}{'avg P&L':>11}{'tot P&L':>11}")
    print("-" * 51)

    for label, lo, hi in BUCKETS:
        sub = [
            r for r in valid
            if r["fill_sec"] >= lo and (hi is None or r["fill_sec"] < hi)
        ]
        if not sub:
            print(f"{label:<12}{0:>4}{'-':>6}{'-':>7}{'-':>11}{'-':>11}")
            continue
        n = len(sub)
        wins = sum(1 for r in sub if (r["pnl_usd"] or 0) > 0)
        tot = sum((r["pnl_usd"] or 0) for r in sub)
        print(
            f"{label:<12}{n:>4}{wins:>6}{wins / n * 100:>6.0f}%"
            f"{tot / n:>+11.2f}{tot:>+11.2f}"
        )

    overall_wins = sum(1 for r in valid if (r["pnl_usd"] or 0) > 0)
    overall_tot = sum((r["pnl_usd"] or 0) for r in valid)
    print("-" * 51)
    print(
        f"{'ALL':<12}{len(valid):>4}{overall_wins:>6}"
        f"{overall_wins / len(valid) * 100:>6.0f}%"
        f"{overall_tot / len(valid):>+11.2f}{overall_tot:>+11.2f}\n"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="live trades (default: paper)")
    args = ap.parse_args()
    main(paper=not args.live)
