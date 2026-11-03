"""Daily lineup optimization: fill the startable slots with the best eligible
players who actually have a game.

This is an assignment problem (players -> slots, maximize total value), solved
exactly with the Hungarian algorithm rather than greedily, because greedy
position filling gets this wrong in exactly the cases you care about: your best
player being the only one who can fill C, so slotting him at Util strands the
slot.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from .players import Player
from .schedule import Schedule

ALL_POSITIONS = {"PG", "SG", "SF", "PF", "C", "G", "F"}
# A slot accepts a player if the player lists the slot, or lists any of these.
SLOT_GROUPS: dict[str, set[str]] = {
    "G": {"PG", "SG", "G"},
    "F": {"SF", "PF", "F"},
    "Util": ALL_POSITIONS,
    "UTIL": ALL_POSITIONS,
}

INELIGIBLE = 1e6


def eligible(player: Player, slot: str) -> bool:
    if slot in {"Util", "UTIL"}:
        return True
    if slot in player.positions:
        return True
    return bool(SLOT_GROUPS.get(slot, set()) & set(player.positions))


@dataclass
class LineupPlan:
    date: dt.date
    starters: list[tuple[str, Player]] = field(default_factory=list)
    bench: list[Player] = field(default_factory=list)
    inactive: list[Player] = field(default_factory=list)  # no game today
    unfilled: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_value(self) -> float:
        return sum(p.blended_value for _, p in self.starters)

    def starter_ids(self) -> set[int]:
        return {p.pid for _, p in self.starters}


def _candidates(
    players: list[Player], date: dt.date, schedule: Schedule, include_questionable: bool
) -> tuple[list[Player], list[Player], list[Player]]:
    """Split the roster into playable, no-game, and ruled-out groups."""
    playable: list[Player] = []
    no_game: list[Player] = []
    ruled_out: list[Player] = []
    for player in players:
        if player.is_out:
            ruled_out.append(player)
        elif player.is_questionable and not include_questionable:
            ruled_out.append(player)
        elif not schedule.plays_on(player.team, date):
            no_game.append(player)
        else:
            playable.append(player)
    return playable, no_game, ruled_out


def optimize(
    players: list[Player],
    slots: list[str],
    date: dt.date | None = None,
    schedule: Schedule | None = None,
    include_questionable: bool = True,
) -> LineupPlan:
    """Best legal assignment of players to startable slots for one date."""
    date = date or dt.date.today()
    schedule = schedule or Schedule(available=False)
    plan = LineupPlan(date=date)

    if not schedule.available:
        plan.notes.append(
            "NBA schedule unavailable - every healthy player treated as playing."
        )

    playable, no_game, ruled_out = _candidates(
        players, date, schedule, include_questionable
    )
    plan.inactive = sorted(no_game, key=lambda p: -p.blended_value)
    if not slots:
        plan.notes.append("No startable slots found in league settings.")
        return plan
    if not playable:
        plan.unfilled = list(slots)
        plan.notes.append("Nobody on the roster is both healthy and playing today.")
        plan.bench = sorted(ruled_out, key=lambda p: -p.blended_value)
        return plan

    # Shift values positive so that leaving a slot empty is never "cheaper"
    # than filling it with a below-average player.
    offset = min((p.blended_value for p in playable), default=0.0)
    cost = np.full((len(playable), len(slots)), INELIGIBLE, dtype=float)
    for i, player in enumerate(playable):
        for j, slot in enumerate(slots):
            if eligible(player, slot):
                cost[i, j] = -(player.blended_value - offset + 1.0)

    rows, cols = linear_sum_assignment(cost)
    assigned: set[int] = set()
    for i, j in zip(rows, cols):
        if cost[i, j] >= INELIGIBLE / 2:
            continue  # forced pairing of an ineligible player; not a real start
        plan.starters.append((slots[j], playable[i]))
        assigned.add(i)

    plan.starters.sort(key=lambda pair: (slots.index(pair[0]), -pair[1].blended_value))

    # A slot name can appear several times (two Util, three G), so subtract
    # filled slots one at a time rather than comparing sets.
    remaining = list(slots)
    for slot, _ in plan.starters:
        if slot in remaining:
            remaining.remove(slot)
    plan.unfilled = remaining

    plan.bench = sorted(
        [p for i, p in enumerate(playable) if i not in assigned] + ruled_out,
        key=lambda p: -p.blended_value,
    )

    for _, player in plan.starters:
        if player.is_questionable:
            plan.notes.append(f"{player.name} is {player.status} - confirm before lock.")
    return plan


def fillable_slots(players: list[Player], slots: list[str]) -> int:
    """How many slots this player set can cover, ignoring value and schedule.

    Used to catch a swap that would leave you unable to field a legal lineup
    (dropping your only center, say).
    """
    if not players or not slots:
        return 0
    cost = np.full((len(players), len(slots)), INELIGIBLE, dtype=float)
    for i, player in enumerate(players):
        for j, slot in enumerate(slots):
            if eligible(player, slot):
                cost[i, j] = -1.0
    rows, cols = linear_sum_assignment(cost)
    return sum(1 for i, j in zip(rows, cols) if cost[i, j] < INELIGIBLE / 2)
