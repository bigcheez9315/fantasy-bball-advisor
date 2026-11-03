"""Rendering: terminal tables, a markdown record, and machine-readable JSON."""
from __future__ import annotations

import datetime as dt


from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analysis import Analysis
from .players import WINDOW_LABELS, WINDOW_ORDER, Player
from .waivers import Swap, format_delta_line

console = Console()


def _value_color(value: float) -> str:
    if value >= 1.0:
        return "bright_green"
    if value >= 0.25:
        return "green"
    if value <= -1.0:
        return "red"
    if value <= -0.25:
        return "yellow"
    return "white"


def _trend_text(player: Player) -> Text:
    if abs(player.trend) < 0.3:
        return Text("flat", style="dim")
    arrow = "up" if player.trend > 0 else "down"
    style = "bright_green" if player.trend > 0 else "red"
    return Text(f"{arrow} {player.trend:+.2f}", style=style)


def _status_text(player: Player) -> Text:
    if player.is_out:
        return Text(player.status or "OUT", style="bold red")
    if player.is_questionable:
        return Text(player.status, style="yellow")
    return Text("-", style="dim")


# --------------------------------------------------------------------------
# terminal
# --------------------------------------------------------------------------

def print_header(analysis: Analysis) -> None:
    weights = analysis.weights.normalized()
    lines = [
        f"[bold]{analysis.league_name or analysis.league_key}[/bold]  "
        f"({analysis.league_key})",
        f"Date: {analysis.date.isoformat()}    Mode: [cyan]{analysis.mode}[/cyan]",
        f"Categories: {', '.join(analysis.cats)}",
        f"Lineup slots: {', '.join(analysis.slots)}",
        f"Valuation pool: {analysis.pool_size} rostered players   "
        f"Window weights: L7 {weights['last7']:.0%} / L30 {weights['last30']:.0%} / "
        f"Season {weights['season']:.0%}",
    ]
    if analysis.mode == "needs":
        tilt = ", ".join(
            f"{cat} {weight:.2f}" for cat, weight in sorted(analysis.cat_weights.items())
        )
        lines.append(f"Category weights: {tilt}")
    console.print(Panel("\n".join(lines), title="9-cat advisor", border_style="blue"))


def print_waivers(analysis: Analysis) -> None:
    report = analysis.waivers
    console.print()
    console.rule("[bold]Add / drop suggestions")

    if not report.swaps:
        console.print(
            "[yellow]No swap clears the improvement threshold.[/yellow] "
            "Your roster is ahead of what the wire offers right now.\n"
        )
    for index, swap in enumerate(report.swaps, start=1):
        _print_swap(index, swap)

    if report.watchlist:
        table = Table(
            title="Watchlist (good available players, no clear drop)",
            title_style="bold",
            header_style="dim",
        )
        table.add_column("Player")
        table.add_column("Team")
        table.add_column("Pos")
        table.add_column("Value", justify="right")
        table.add_column("Trend", justify="right")
        table.add_column("Own%", justify="right")
        for player in report.watchlist:
            table.add_row(
                player.name,
                player.team,
                "/".join(player.positions[:3]),
                Text(f"{player.blended_value:+.2f}", style=_value_color(player.blended_value)),
                _trend_text(player),
                f"{player.pct_owned:.0f}",
            )
        console.print(table)

    if report.stash_candidates:
        names = ", ".join(
            f"{p.name} ({p.status}, season {p.value.get('season', 0):+.2f})"
            for p in report.stash_candidates
        )
        console.print(
            Panel(
                names,
                title="Injured stashes on the wire",
                border_style="magenta",
            )
        )


def _print_swap(index: int, swap: Swap) -> None:
    add, drop = swap.add, swap.drop
    claim = " [magenta](waiver claim)[/magenta]" if add.needs_waiver_claim else ""
    headline = (
        f"[bold]{index}. ADD [bright_green]{add.name}[/bright_green]"
        f"{claim}  /  DROP [red]{drop.name}[/red][/bold]   "
        f"net [bold]{swap.net:+.2f}[/bold] z"
    )
    body = [
        headline,
        f"   {add.name}: {add.blended_value:+.2f} z   "
        f"{drop.name}: {drop.blended_value:+.2f} z",
        f"   categories: {format_delta_line(swap.deltas, limit=6)}",
    ]
    for reason in swap.reasons:
        body.append(f"   - {reason}")
    for warning in swap.warnings:
        body.append(f"   [yellow]! {warning}[/yellow]")
    console.print("\n".join(body))
    console.print()


def print_roster(analysis: Analysis) -> None:
    console.print()
    console.rule("[bold]Your roster")
    table = Table(header_style="bold")
    table.add_column("Player")
    table.add_column("Team")
    table.add_column("Pos")
    table.add_column("St")
    for window in WINDOW_ORDER:
        table.add_column(WINDOW_LABELS[window], justify="right")
    table.add_column("Blend", justify="right")
    table.add_column("Trend", justify="right")
    table.add_column("G/7d", justify="right")

    for player in analysis.my_sorted():
        row = [
            player.name,
            player.team,
            "/".join(player.positions[:3]),
            _status_text(player),
        ]
        for window in WINDOW_ORDER:
            value = player.value.get(window)
            row.append(
                Text(f"{value:+.2f}", style=_value_color(value))
                if value is not None
                else Text("-", style="dim")
            )
        row.append(
            Text(f"{player.blended_value:+.2f}", style=_value_color(player.blended_value))
        )
        row.append(_trend_text(player))
        games = (
            analysis.schedule.game_count(player.team, analysis.date, 7)
            if analysis.schedule.available
            else None
        )
        row.append(str(games) if games is not None else "-")
        table.add_row(*row)
    console.print(table)


def print_lineup(analysis: Analysis) -> None:
    plan = analysis.lineup
    if plan is None:
        return
    console.print()
    console.rule(f"[bold]Lineup for {plan.date.isoformat()}")

    if not plan.starters:
        console.print("[yellow]No startable lineup could be built.[/yellow]")
    else:
        table = Table(header_style="bold")
        table.add_column("Slot")
        table.add_column("Start")
        table.add_column("Team")
        table.add_column("Value", justify="right")
        table.add_column("Trend", justify="right")
        for slot, player in plan.starters:
            table.add_row(
                slot,
                player.name + (f" ({player.status})" if player.status else ""),
                player.team,
                Text(f"{player.blended_value:+.2f}", style=_value_color(player.blended_value)),
                _trend_text(player),
            )
        console.print(table)

    if plan.unfilled:
        console.print(f"[yellow]Unfilled slots:[/yellow] {', '.join(plan.unfilled)}")
    if plan.bench:
        console.print(
            "[dim]Sit (has game, lost the slot):[/dim] "
            + ", ".join(
                f"{p.name} {p.blended_value:+.2f}" for p in plan.bench if not p.is_out
            )
        )
    if plan.inactive:
        console.print(
            "[dim]No game today:[/dim] " + ", ".join(p.name for p in plan.inactive)
        )
    out = [p for p in plan.bench if p.is_out]
    if out:
        console.print(
            "[red]Out/injured:[/red] "
            + ", ".join(f"{p.name} ({p.status})" for p in out)
        )
    for note in plan.notes:
        console.print(f"[yellow]! {note}[/yellow]")


def print_profile(analysis: Analysis) -> None:
    if not analysis.team_profile:
        return
    console.print()
    console.rule("[bold]Category profile (sum of z for your top 10)")
    table = Table(header_style="bold", show_lines=False)
    for cat in analysis.cats:
        table.add_column(cat, justify="right")
    table.add_row(
        *[
            Text(
                f"{analysis.team_profile.get(cat, 0.0):+.1f}",
                style=_value_color(analysis.team_profile.get(cat, 0.0) / 3),
            )
            for cat in analysis.cats
        ]
    )
    console.print(table)
    console.print(
        "[dim]Positive = stronger than an average roster in this league. "
        "Near zero = contested, where a marginal add swings the most matchups."
        "[/dim]"
    )


def print_footer(analysis: Analysis) -> None:
    if analysis.notes:
        console.print()
        for note in analysis.notes:
            console.print(f"[dim]note: {note}[/dim]")
    console.print(
        f"\n[dim]{analysis.api_calls} uncached Yahoo call(s) this run. "
        "Re-runs within the cache window are free; pass --fresh to bypass.[/dim]"
    )


def print_full(analysis: Analysis) -> None:
    print_header(analysis)
    print_waivers(analysis)
    print_lineup(analysis)
    print_roster(analysis)
    print_profile(analysis)
    print_footer(analysis)


# --------------------------------------------------------------------------
# machine + archive formats
# --------------------------------------------------------------------------

def to_dict(analysis: Analysis) -> dict:
    """Compact JSON view, for piping into other tools or diffing runs."""

    def player_dict(player: Player) -> dict:
        return {
            "player_id": player.pid,
            "name": player.name,
            "team": player.team,
            "positions": player.positions,
            "status": player.status,
            "pct_owned": round(player.pct_owned, 1),
            "source": player.source,
            "value_by_window": {k: round(v, 3) for k, v in player.value.items()},
            "blended_value": round(player.blended_value, 3),
            "trend": round(player.trend, 3),
            "games_played": {
                window: player.window(window).games for window in WINDOW_ORDER
            },
            "z": {cat: round(v, 3) for cat, v in player.blended_z.items()},
        }

    return {
        "league": {
            "key": analysis.league_key,
            "name": analysis.league_name,
            "categories": analysis.cats,
            "slots": analysis.slots,
            "pool_size": analysis.pool_size,
        },
        "date": analysis.date.isoformat(),
        "mode": analysis.mode,
        "window_weights": analysis.weights.normalized(),
        "category_weights": {k: round(v, 3) for k, v in analysis.cat_weights.items()},
        "roster": [player_dict(p) for p in analysis.my_sorted()],
        "team_profile": {k: round(v, 2) for k, v in analysis.team_profile.items()},
        "replacement_level": round(analysis.waivers.replacement_level, 3),
        "suggestions": [
            {
                "add": player_dict(s.add),
                "drop": player_dict(s.drop),
                "net": round(s.net, 3),
                "category_deltas": {k: round(v, 3) for k, v in s.deltas.items()},
                "reasons": s.reasons,
                "warnings": s.warnings,
                "roster_legal": s.legal,
            }
            for s in analysis.waivers.swaps
        ],
        "watchlist": [player_dict(p) for p in analysis.waivers.watchlist],
        "stashes": [player_dict(p) for p in analysis.waivers.stash_candidates],
        "lineup": (
            {
                "date": analysis.lineup.date.isoformat(),
                "starters": [
                    {"slot": slot, "name": p.name, "value": round(p.blended_value, 3)}
                    for slot, p in analysis.lineup.starters
                ],
                "sit": [p.name for p in analysis.lineup.bench if not p.is_out],
                "no_game": [p.name for p in analysis.lineup.inactive],
                "out": [
                    f"{p.name} ({p.status})"
                    for p in analysis.lineup.bench
                    if p.is_out
                ],
                "unfilled": analysis.lineup.unfilled,
                "notes": analysis.lineup.notes,
            }
            if analysis.lineup
            else None
        ),
        "notes": analysis.notes,
    }


def to_markdown(analysis: Analysis) -> str:
    """A dated record of the run, for the reports/ folder."""
    lines = [
        f"# Waiver report - {analysis.date.isoformat()}",
        "",
        f"League: **{analysis.league_name}** (`{analysis.league_key}`)  ",
        f"Mode: `{analysis.mode}` | Categories: {', '.join(analysis.cats)} | "
        f"Pool: {analysis.pool_size} rostered players",
        "",
        "## Suggested moves",
        "",
    ]
    if not analysis.waivers.swaps:
        lines.append("_No swap cleared the improvement threshold._")
    for index, swap in enumerate(analysis.waivers.swaps, start=1):
        lines.append(
            f"### {index}. ADD {swap.add.name} / DROP {swap.drop.name} "
            f"({swap.net:+.2f} z)"
        )
        lines.append("")
        lines.append(
            f"- {swap.add.name}: `{swap.add.blended_value:+.2f}` z "
            f"(L7 {swap.add.value.get('last7', float('nan')):+.2f}, "
            f"L30 {swap.add.value.get('last30', float('nan')):+.2f}, "
            f"season {swap.add.value.get('season', float('nan')):+.2f})"
        )
        lines.append(
            f"- {swap.drop.name}: `{swap.drop.blended_value:+.2f}` z "
            f"(L7 {swap.drop.value.get('last7', float('nan')):+.2f}, "
            f"L30 {swap.drop.value.get('last30', float('nan')):+.2f}, "
            f"season {swap.drop.value.get('season', float('nan')):+.2f})"
        )
        lines.append(f"- Categories: {format_delta_line(swap.deltas, limit=9)}")
        for reason in swap.reasons:
            lines.append(f"- {reason}")
        for warning in swap.warnings:
            lines.append(f"- **Caution:** {warning}")
        lines.append("")

    if analysis.waivers.watchlist:
        lines += ["## Watchlist", "", "| Player | Team | Pos | Value | Trend | Own% |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for player in analysis.waivers.watchlist:
            lines.append(
                f"| {player.name} | {player.team} | {'/'.join(player.positions[:3])} "
                f"| {player.blended_value:+.2f} | {player.trend:+.2f} "
                f"| {player.pct_owned:.0f} |"
            )
        lines.append("")

    lines += ["## Roster", "", "| Player | Team | Pos | St | L7 | L30 | Season | Blend | Trend |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for player in analysis.my_sorted():
        cells = [
            player.name,
            player.team,
            "/".join(player.positions[:3]),
            player.status or "-",
        ]
        for window in WINDOW_ORDER:
            value = player.value.get(window)
            cells.append(f"{value:+.2f}" if value is not None else "-")
        cells.append(f"{player.blended_value:+.2f}")
        cells.append(f"{player.trend:+.2f}")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    if analysis.lineup and analysis.lineup.starters:
        lines += [f"## Lineup for {analysis.lineup.date.isoformat()}", "",
                  "| Slot | Player | Value |", "| --- | --- | --- |"]
        for slot, player in analysis.lineup.starters:
            lines.append(f"| {slot} | {player.name} | {player.blended_value:+.2f} |")
        lines.append("")
        if analysis.lineup.bench:
            sitting = ", ".join(
                p.name for p in analysis.lineup.bench if not p.is_out
            )
            lines.append(f"Sit: {sitting or 'nobody'}")
        if analysis.lineup.inactive:
            lines.append(
                "No game: " + ", ".join(p.name for p in analysis.lineup.inactive)
            )
        lines.append("")

    lines += ["## Category profile", "",
              "| " + " | ".join(analysis.cats) + " |",
              "| " + " | ".join("---" for _ in analysis.cats) + " |",
              "| " + " | ".join(
                  f"{analysis.team_profile.get(c, 0.0):+.1f}" for c in analysis.cats
              ) + " |",
              ""]

    if analysis.notes:
        lines += ["## Notes", ""] + [f"- {note}" for note in analysis.notes] + [""]

    lines += [
        "## Discussion",
        "",
        "_Notes on what you actually did and why._",
        "",
    ]
    return "\n".join(lines)


def save_markdown(analysis: Analysis, directory) -> "object":
    from pathlib import Path

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = analysis.date.isoformat()
    path = directory / f"{stamp}-waivers.md"
    suffix = 1
    while path.exists():
        path = directory / f"{stamp}-waivers-{suffix}.md"
        suffix += 1
    path.write_text(to_markdown(analysis), encoding="utf-8")
    return path
