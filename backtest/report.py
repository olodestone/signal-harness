"""
Comparison report — prints the EXACT /edge + /diagnose output the Telegram bot would
show, per strategy run_tag, then a cross-strategy ranking + recommendation.

    DATABASE_URL=postgresql://postgres:bt@localhost:5433/bt python -m backtest.report

Ranking (the harness has no execution, so Layer-1 fill + Layer-4 P&L are empty by
design — strategies are judged on what it DOES measure):
  1. Layer-3 win-when-hit %   (gated: n_resolved >= 10 and entry-hit >= 30%)
  2. Layer-2 direction accuracy dir@4h, then dir@24h
  3. resolved sample size (tie-break)
"""
from __future__ import annotations

import argparse
from typing import Dict, List

import analytics
import performance as perf
from .strategies import REGISTRY

PAPER = True
N_RESOLVED_GATE = 10
ENTRY_HIT_GATE = 30.0


def _pct(num: int, den: int):
    return (num / den * 100) if den else None


def _metrics(tag: str, days: int) -> Dict:
    funnel = perf.edge_funnel(PAPER, days, tag)
    setup = perf.edge_setup_quality(PAPER, days, tag)
    dirs = perf.edge_direction_stats(PAPER, days, run_tag=tag)["overall"]

    n_resolved = int(setup["n_resolved"] or 0)
    n_hit = int(setup["n_entry_hit"] or 0)
    n_wins = int(setup["n_wins"] or 0)
    n_4h, ok_4h = int(dirs["n_4h"] or 0), int(dirs["ok_4h"] or 0)
    n_24h, ok_24h = int(dirs["n_24h"] or 0), int(dirs["ok_24h"] or 0)

    return {
        "tag": tag,
        "total": int(funnel["total"] or 0),
        "n_resolved": n_resolved,
        "entry_hit_pct": _pct(n_hit, n_resolved),
        "win_hit_pct": _pct(n_wins, n_hit),
        "dir4": _pct(ok_4h, n_4h),
        "dir24": _pct(ok_24h, n_24h),
        "n_4h": n_4h,
        "n_hit": n_hit,
    }


def _rank_key(m: Dict):
    qualified = (m["n_resolved"] >= N_RESOLVED_GATE
                 and (m["entry_hit_pct"] or 0) >= ENTRY_HIT_GATE)
    return (
        1 if qualified else 0,
        m["win_hit_pct"] or -1,
        m["dir4"] or -1,
        m["dir24"] or -1,
        m["n_resolved"],
    )


def _fmt(v, suffix="%"):
    return f"{v:.0f}{suffix}" if v is not None else "—"


def build_ranking(tags: List[str], days: int) -> str:
    mets = [_metrics(t, days) for t in tags]
    ranked = sorted(mets, key=_rank_key, reverse=True)

    lines = [
        "",
        "═" * 72,
        f"  STRATEGY RANKING  (paper · last {days}d · same universe)",
        "═" * 72,
        f"  {'strategy':<10} {'signals':>7} {'resolved':>8} {'entry-hit':>9} "
        f"{'win/hit':>8} {'dir@4h':>7} {'dir@24h':>8}",
        "  " + "-" * 68,
    ]
    for m in ranked:
        lines.append(
            f"  {m['tag']:<10} {m['total']:>7} {m['n_resolved']:>8} "
            f"{_fmt(m['entry_hit_pct']):>9} {_fmt(m['win_hit_pct']):>8} "
            f"{_fmt(m['dir4']):>7} {_fmt(m['dir24']):>8}"
        )

    lines += ["  " + "-" * 68]
    best = ranked[0]
    qualified = [m for m in ranked
                 if m["n_resolved"] >= N_RESOLVED_GATE and (m["entry_hit_pct"] or 0) >= ENTRY_HIT_GATE]
    if qualified and qualified[0] is best:
        lines.append(
            f"  🏆 BEST: {best['tag']}  —  win-when-hit {_fmt(best['win_hit_pct'])} "
            f"on {best['n_hit']} entry-hit setups, dir@4h {_fmt(best['dir4'])} "
            f"({best['n_resolved']} resolved)."
        )
    elif best["dir4"] is not None:
        lines.append(
            f"  ⚠ No strategy cleared the gates (need ≥{N_RESOLVED_GATE} resolved & "
            f"≥{ENTRY_HIT_GATE:.0f}% entry-hit)."
        )
        lines.append(
            f"  Best on direction only: {best['tag']} (dir@4h {_fmt(best['dir4'])}). "
            f"Treat as low-confidence — widen window or loosen its filters for more sample."
        )
    else:
        lines.append("  ⚠ Not enough resolved data to rank — extend --days or check the seed run.")
    lines.append("═" * 72)
    return "\n".join(lines)


MONTH_MIN_SAMPLE = 20      # below this, a month/strategy cell is too thin to trust


def build_monthly_matrix(tags: List[str]) -> str:
    """Month-by-month win-when-hit% per strategy, so you can see which regimes
    favour which strategy (validates whether an edge is real or a one-month artifact).
    Win-when-hit = wins / resolved (entry-hit is 100% for market entries)."""
    with perf._cur() as cur:
        cur.execute(
            """
            SELECT to_char(date_trunc('month', signal_time::timestamp), 'YYYY-MM') AS ym,
                   run_tag,
                   COUNT(*) FILTER (WHERE setup_outcome IS NOT NULL) AS resolved,
                   COUNT(*) FILTER (WHERE setup_outcome = 'win')     AS wins,
                   COUNT(*) FILTER (WHERE dir_4h IS NOT NULL)        AS n4,
                   COUNT(*) FILTER (WHERE dir_4h)                    AS ok4
            FROM signal_log
            WHERE run_tag = ANY(%s)
            GROUP BY ym, run_tag
            ORDER BY ym
            """,
            (tags,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    # index: month -> tag -> (win_pct, resolved, dir4_pct)
    cells: Dict[str, Dict[str, tuple]] = {}
    for r in rows:
        resolved = int(r["resolved"] or 0)
        wins = int(r["wins"] or 0)
        n4, ok4 = int(r["n4"] or 0), int(r["ok4"] or 0)
        win_pct = _pct(wins, resolved)
        dir4 = _pct(ok4, n4)
        cells.setdefault(r["ym"], {})[r["run_tag"]] = (win_pct, resolved, dir4)

    months = sorted(cells)
    wins_per_tag = {t: 0 for t in tags}
    sweep_edge_months: List[str] = []

    out = [
        "",
        "═" * 78,
        "  MONTH-BY-MONTH  ·  win-when-hit %  (breakeven ≈ 33% at RR 2:1)  ·  * = best",
        "  (cells with <%d resolved signals shown as '·')" % MONTH_MIN_SAMPLE,
        "═" * 78,
        "  month    " + "".join(f"{t:>12}" for t in tags),
        "  " + "-" * 74,
    ]
    for ym in months:
        # best strategy this month among sufficiently-sampled cells
        valid = {t: cells[ym].get(t) for t in tags
                 if cells[ym].get(t) and cells[ym][t][1] >= MONTH_MIN_SAMPLE
                 and cells[ym][t][0] is not None}
        best_tag = max(valid, key=lambda t: valid[t][0]) if valid else None
        if best_tag:
            wins_per_tag[best_tag] += 1
        if best_tag == "bt_sweep" and valid["bt_sweep"][0] >= 33.0:
            sweep_edge_months.append(ym)

        line = f"  {ym:<9}"
        for t in tags:
            c = cells[ym].get(t)
            if not c or c[1] < MONTH_MIN_SAMPLE or c[0] is None:
                line += f"{'·':>12}"
            else:
                star = "*" if t == best_tag else " "
                line += f"{c[0]:>9.0f}%{star} "
        out.append(line)

    out += ["  " + "-" * 74]
    out.append("  months won (best win-when-hit): "
               + "  ".join(f"{t}={wins_per_tag[t]}" for t in tags))
    if "bt_sweep" in tags:
        if sweep_edge_months:
            out.append(f"  bt_sweep led AND beat breakeven in: {', '.join(sweep_edge_months)} "
                       f"({len(sweep_edge_months)}/{len(months)} months)")
        else:
            out.append("  bt_sweep did not lead-and-beat-breakeven in any month.")
    out.append("═" * 78)
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--strategies", type=str, default=",".join(REGISTRY))
    ap.add_argument("--monthly", action="store_true",
                    help="print only the month-by-month matrix (for the 1-year run)")
    args = ap.parse_args()
    tags = [t.strip() for t in args.strategies.split(",") if t.strip() in REGISTRY]

    if args.monthly:
        print(build_monthly_matrix(tags))
        print(build_ranking(tags, args.days))
        return

    for tag in tags:
        print("\n" + "#" * 72)
        print(f"#  STRATEGY: {tag}")
        print("#" * 72)
        print(analytics.build_edge_overview(PAPER, days=args.days, run_tag=tag))
        print()
        print(analytics.build_diagnose(PAPER, run_tag=tag))
        print()
        print(analytics.build_edge_group(PAPER, "pairs", days=args.days, run_tag=tag))

    print(build_ranking(tags, args.days))


if __name__ == "__main__":
    main()
