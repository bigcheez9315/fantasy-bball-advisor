"""Rich rendering for the weekly matchup view."""
from __future__ import annotations

from rich.table import Table

from .matchup import PCT_PARTS, MatchupReport, WeekMove, best_per_add, rates
from .report import console


def _print_move(report: MatchupReport, i: int, mv: WeekMove) -> None:
    claim = " (waiver claim)" if mv.add.needs_waiver_claim else ""
    out = " [OUT]" if mv.drop.is_out else ""
    console.print(
        f"  {i}. ADD {mv.add.name} {mv.add.team}{claim} / DROP {mv.drop.name}{out}   "
        f"[bold]+{mv.delta_wins:.2f} cats[/bold]   season value {mv.season_delta:+.2f}   "
        f"live {mv.effective:%a}, {report.games_left(mv.add.team, mv.effective)}g left"
    )
    if mv.swings:
        console.print("     " + ", ".join(
            f"{c} {d:+.0%}" for c, d in sorted(mv.swings.items(), key=lambda x: -abs(x[1]))
        ))


def _fmt(cat: str, value: float) -> str:
    return f"{value:.3f}".lstrip("0") if cat in PCT_PARTS else f"{value:.0f}"


def _pct(made: float, att: float) -> str:
    return f"{made / att:.3f}".lstrip("0") if att else "-"


def print_matchup(report: MatchupReport) -> None:
    m = report.matchup
    console.rule(
        f"Week {m.week}: {m.me.name} vs {m.opp.name}  "
        f"({m.start:%a %b %d} - {m.end:%a %b %d})"
    )
    console.print(
        f"Games left (Yahoo): you {m.me.remaining_games}, them {m.opp.remaining_games}  |  "
        f"Adds used: you {m.me.adds_used}/{report.max_adds}, "
        f"them {m.opp.adds_used}/{report.max_adds}  |  "
        f"Projected starts left: you {report.me_proj.starts}, them {report.opp_proj.starts}"
    )

    table = Table(title="Category outlook (projected to Sunday)")
    for col in ("Cat", "You now", "Opp now", "You proj", "Opp proj", "P(win)", "Status",
                "If opp streams"):
        table.add_column(col, justify="left" if col in ("Cat", "Status") else "right")
    streamed = {r.cat: r for r in report.rows_if_opp_streams}
    for r in report.rows:
        table.add_row(
            r.cat, _fmt(r.cat, r.me_now), _fmt(r.cat, r.opp_now),
            _fmt(r.cat, r.me_proj), _fmt(r.cat, r.opp_proj),
            f"{r.p_win:.0%}", r.label, f"{streamed[r.cat].p_win:.0%}",
        )
    console.print(table)
    exp = sum(r.p_win for r in report.rows)
    exp_s = sum(r.p_win for r in report.rows_if_opp_streams)
    console.print(
        f"Expected categories won: [bold]{exp:.1f}[/bold] of {len(report.rows)}   "
        f"(if opp spends {len(report.opp_streamers)} add(s) on streamers: {exp_s:.1f})"
    )
    if report.opp_streamers:
        console.print("  assumed opp streamers: " + ", ".join(
            f"{p.name} ({p.team}, {report.games_left(p.team)}g)" for p in report.opp_streamers
        ))

    opp = Table(title=f"Opponent roster: {m.opp.name} (per game, L30 or season)")
    for col in ("Player", "Team", "St", "Value", "G left", "PTS", "REB", "AST", "3PM",
                "ST", "BLK", "TO", "FG%", "FT%"):
        opp.add_column(col, justify="left" if col in ("Player", "Team", "St") else "right")
    for p in sorted(report.opp_players, key=lambda p: -p.blended_value):
        r = rates(p)
        opp.add_row(
            p.name, p.team, p.status or "-", f"{p.blended_value:+.2f}",
            str(report.games_left(p.team)),
            *(f"{r.get(c, 0):.1f}" for c in ("PTS", "REB", "AST", "3PTM", "ST", "BLK", "TO")),
            _pct(r.get("FGM", 0), r.get("FGA", 0)), _pct(r.get("FTM", 0), r.get("FTA", 0)),
        )
    console.print(opp)

    for label, moves in (("Your", report.my_moves_this_week),
                         ("Opponent's", report.opp_moves_this_week)):
        console.print(f"\n[bold]{label} adds/drops this week[/bold]")
        if not moves:
            console.print("  none")
        for mv in moves:
            src = f" (from {mv.source})" if mv.kind == "add" else ""
            console.print(
                f"  {mv.when:%a %m/%d %I:%M%p} ET  {mv.kind.upper():4} {mv.player} {mv.team}{src}"
            )

    console.rule("Moves for THIS week (ranked by change in expected categories won)")
    if report.my_adds_left == 0:
        console.print("You have no adds left this week.")
    elif not report.best_moves:
        console.print("No add/drop raises expected categories won by more than 0.05.")
    else:
        sections = (
            ("Keeper-safe (you don't give up long-term value)",
             best_per_add(report.best_moves, keeper_safe=True)),
            ("Pure streams (best for this week, costs long-term value)",
             best_per_add(report.best_moves)),
        )
        for title, moves in sections:
            console.print(f"\n[bold]{title}[/bold]")
            if not moves:
                console.print("  none clear the bar")
            for i, mv in enumerate(moves, 1):
                _print_move(report, i, mv)
    console.print(
        f"\nAdds left: {report.my_adds_left}. An add spent now can't cover a "
        "later injury this week."
    )
