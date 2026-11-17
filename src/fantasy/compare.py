"""Side-by-side look at a few players: role, shot volume, and recent games.

Built for the "keep him or the other guy?" call, where the question is less
"who has the better z-score" than "is his role holding up": minutes and shot
attempts (FGA) are the role signals; 3s, steals and blocks are the specific
categories that make a player worth a roster spot.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from rich.table import Table

from .players import WINDOW_ORDER, Player, attach_stats
from .report import console
from .valuation import score_players

COLUMNS = ["MIN", "FGA", "PTS", "3PTM", "REB", "AST", "ST", "BLK", "TO"]
LABELS = {"last7": "L7", "last14": "L14", "last30": "L30", "season": "Season"}


@dataclass
class Compared:
    player: Player
    games: list[tuple[dt.date, dict]] = field(default_factory=list)  # newest first


def per_game(totals: dict, games: float) -> dict[str, float]:
    """Raw per-game numbers (no role adjustment), plus FG% and 3PT% from totals."""
    if games <= 0:
        return {}
    out = {c: totals.get(c, 0.0) / games for c in COLUMNS}
    fga, threes_a = totals.get("FGA", 0.0), totals.get("3PTA", 0.0)
    out["FG%"] = totals.get("FGM", 0.0) / fga if fga else 0.0
    out["3PT%"] = totals.get("3PTM", 0.0) / threes_a if threes_a else 0.0
    return out


def collect(client: Any, analysis: Any, names: list[str], days: int = 7) -> list[Compared]:
    """Look players up by name (any team), value them on the league's pools,
    and pull their last `days` of game lines."""
    known = {p.name.casefold(): p for p in analysis.my_players + analysis.available}
    players: list[Player] = []
    for name in names:
        hit = known.get(name.casefold())
        if hit is None:
            found = client.find_player(name)
            if found is None:
                console.print(f"[yellow]No Yahoo player matches {name!r}.[/yellow]")
                continue
            hit = Player(**found)
        players.append(hit)

    by_id = {p.pid: p for p in players}
    fresh = {pid: p for pid, p in by_id.items() if not p.windows}
    if fresh:
        attach_stats(
            fresh, {w: client.stats(fresh.keys(), w) for w in WINDOW_ORDER}, analysis.cats
        )
        score_players(
            list(fresh.values()), analysis.pools, analysis.cats,
            analysis.weights, analysis.cat_weights,
        )

    out = [Compared(p) for p in players]
    for offset in range(1, days + 1):
        day = analysis.date - dt.timedelta(days=offset)
        lines = client.day_stats(by_id.keys(), day)
        for item in out:
            row = lines.get(item.player.pid) or {}
            if row.get("GP", 0) > 0:
                item.games.append((day, row))
    return out


def _fmt(col: str, value: float) -> str:
    if col in ("FG%", "3PT%"):
        return f"{value:.3f}".lstrip("0") if value else "-"
    return f"{value:.1f}"


def print_compare(items: list[Compared]) -> None:
    cols = COLUMNS + ["FG%", "3PT%"]
    for item in items:
        p = item.player
        console.rule(
            f"{p.name} ({p.team})   value {p.blended_value:+.2f}   "
            f"trend {p.trend:+.2f}"
        )
        table = Table(title="Per game by window", title_justify="left")
        table.add_column("Window")
        table.add_column("GP", justify="right")
        for col in cols:
            table.add_column(col, justify="right")
        table.add_column("Value", justify="right")
        for window in WINDOW_ORDER:
            w = p.window(window)
            rates = per_game(w.totals, w.games)
            if not rates:
                continue
            table.add_row(
                LABELS[window], f"{w.games:.0f}",
                *(_fmt(c, rates[c]) for c in cols),
                f"{p.value.get(window, 0.0):+.2f}",
            )
        console.print(table)

        log = Table(title="Recent games", title_justify="left")
        log.add_column("Date")
        for col in cols:
            log.add_column(col, justify="right")
        for day, row in item.games:
            rates = per_game(row, 1)
            log.add_row(f"{day:%a %m/%d}", *(_fmt(c, rates[c]) for c in cols))
        if item.games:
            console.print(log)
        else:
            console.print("  no games in the window")
    console.print(
        "\nRole signals: MIN and FGA. If one player's shots are crowding out "
        "another's, it shows up as falling FGA at steady minutes."
    )
