"""Lineup assignment: position eligibility, scheduling, and health filters."""
from __future__ import annotations

import datetime as dt

from fantasy.lineup import eligible, fillable_slots, optimize
from fantasy.players import Player
from fantasy.schedule import Schedule

TODAY = dt.date(2026, 11, 4)


def make(pid, name, positions, value, team="BOS", status=""):
    player = Player(pid=pid, name=name, positions=list(positions), team=team, status=status)
    player.blended_value = value
    return player


def all_playing(*teams):
    return Schedule(by_date={TODAY.isoformat(): set(teams or ["BOS"])})


def test_composite_slot_eligibility():
    guard = make(1, "guard", ["PG", "SG"], 1.0)
    forward = make(2, "forward", ["SF"], 1.0)
    center = make(3, "center", ["C"], 1.0)

    assert eligible(guard, "G")
    assert eligible(guard, "PG")
    assert not eligible(guard, "F")
    assert not eligible(guard, "C")
    assert eligible(forward, "F")
    assert eligible(center, "C")
    # Util takes anyone.
    for player in (guard, forward, center):
        assert eligible(player, "Util")


def test_scarce_position_is_not_stranded():
    """The case greedy filling gets wrong.

    The best player is the only center. Filling Util first by value would put
    him there and leave C empty. The optimizer must put him at C.
    """
    slots = ["C", "Util"]
    star_center = make(1, "star C", ["C"], 3.0)
    good_guard = make(2, "good G", ["PG"], 2.0)

    plan = optimize([star_center, good_guard], slots, date=TODAY, schedule=all_playing("BOS"))
    assignment = {slot: player.name for slot, player in plan.starters}
    assert assignment == {"C": "star C", "Util": "good G"}
    assert not plan.unfilled


def test_optimizer_maximizes_total_value():
    slots = ["PG", "G", "Util"]
    players = [
        make(1, "elite PG", ["PG"], 3.0),
        make(2, "good SG", ["SG"], 2.0),
        make(3, "bench PG", ["PG"], 0.5),
        make(4, "weak C", ["C"], 0.1),
    ]
    plan = optimize(players, slots, date=TODAY, schedule=all_playing("BOS"))
    started = {p.name for _, p in plan.starters}
    # Best three who can legally fill PG, G, Util.
    assert started == {"elite PG", "good SG", "bench PG"}
    assert plan.total_value == 5.5


def test_players_without_a_game_are_not_started():
    slots = ["Util", "Util"]
    playing = make(1, "playing", ["PG"], 1.0, team="BOS")
    idle = make(2, "idle", ["PG"], 5.0, team="LAL")  # better, but no game

    schedule = Schedule(by_date={TODAY.isoformat(): {"BOS"}})
    plan = optimize([playing, idle], slots, date=TODAY, schedule=schedule)

    assert [p.name for _, p in plan.starters] == ["playing"]
    assert [p.name for p in plan.inactive] == ["idle"]
    assert plan.unfilled == ["Util"]


def test_out_players_are_excluded_and_questionable_are_flagged():
    slots = ["Util"]
    out = make(1, "injured", ["PG"], 5.0, status="O")
    gtd = make(2, "gametime", ["PG"], 1.0, status="GTD")

    plan = optimize([out, gtd], slots, date=TODAY, schedule=all_playing("BOS"))
    assert [p.name for _, p in plan.starters] == ["gametime"]
    assert any("gametime" in note for note in plan.notes)

    strict = optimize(
        [out, gtd], slots, date=TODAY, schedule=all_playing("BOS"),
        include_questionable=False,
    )
    assert not strict.starters


def test_missing_schedule_degrades_to_starting_everyone():
    slots = ["Util"]
    player = make(1, "anyone", ["PG"], 1.0, team="LAL")
    plan = optimize([player], slots, date=TODAY, schedule=Schedule(available=False))
    assert [p.name for _, p in plan.starters] == ["anyone"]
    assert any("schedule unavailable" in note.lower() for note in plan.notes)


def test_duplicate_slots_are_counted_individually():
    slots = ["Util", "Util", "Util"]
    players = [make(i, f"p{i}", ["PG"], 1.0) for i in range(2)]
    plan = optimize(players, slots, date=TODAY, schedule=all_playing("BOS"))
    assert len(plan.starters) == 2
    assert plan.unfilled == ["Util"]  # exactly one left over, not all three


def test_fillable_slots_detects_coverage_loss():
    slots = ["C", "G", "Util"]
    center = make(1, "center", ["C"], 1.0)
    guard = make(2, "guard", ["PG"], 1.0)
    wing = make(3, "wing", ["SF"], 1.0)

    assert fillable_slots([center, guard, wing], slots) == 3
    # Drop the only center and nothing can cover C.
    assert fillable_slots([guard, wing], slots) == 2
