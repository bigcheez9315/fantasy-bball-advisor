"""Waiver recommendation behaviour and its guardrails."""
from __future__ import annotations

import datetime as dt

import pytest

from fantasy.players import Player, Window
from fantasy.schedule import Schedule
from fantasy.waivers import format_delta_line, recommend

TODAY = dt.date(2026, 11, 4)
SLOTS = ["PG", "SG", "G", "SF", "PF", "F", "C", "Util", "Util"]


def make(
    pid, name, value, positions=("PG", "SG"), source="fa", status="",
    trend=0.0, z=None, team="BOS", pct_owned=0.0, games=4.0,
):
    player = Player(
        pid=pid, name=name, positions=list(positions), source=source,
        status=status, team=team, pct_owned=pct_owned,
    )
    player.blended_value = value
    player.trend = trend
    player.blended_z = z or {"PTS": value}
    player.value = {"last7": value, "last30": value, "season": value}
    player.windows = {
        w: Window(games=games, per_game={"PTS": 10.0, "MIN": 30.0})
        for w in ("last7", "last30", "season")
    }
    return player


def schedule_all():
    return Schedule(
        by_date={
            (TODAY + dt.timedelta(days=d)).isoformat(): {"BOS", "LAL"} for d in range(14)
        }
    )


def test_recommends_the_best_available_over_the_worst_roster_player():
    roster = [make(i, f"mine{i}", v, source="roster") for i, v in enumerate([2.0, 1.0, -0.8], 1)]
    wire = [make(10, "stud", 1.5), make(11, "scrub", -2.0)]

    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY)

    assert len(report.swaps) == 1
    swap = report.swaps[0]
    assert swap.add.name == "stud"
    assert swap.drop.name == "mine3"  # the worst player, not just any
    assert swap.net == pytest.approx(2.3)


def test_small_upgrades_are_suppressed():
    roster = [make(1, "mine", 1.0, source="roster")]
    wire = [make(10, "marginal", 1.2)]
    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY,
                       min_gain=0.5)
    assert report.swaps == []


def test_out_players_are_never_suggested_as_adds():
    roster = [make(1, "mine", -1.0, source="roster")]
    wire = [make(10, "injured stud", 3.0, status="O")]
    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY)
    assert report.swaps == []


def test_injured_wire_stud_is_offered_as_a_stash():
    roster = [make(1, "mine", 0.0, source="roster")]
    wire = [
        make(10, "injured stud", 3.0, status="INJ"),
        make(11, "filler", 0.1),
    ]
    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY)
    assert [p.name for p in report.stash_candidates] == ["injured stud"]


def test_waiver_claim_is_flagged_as_not_a_free_add():
    roster = [make(1, "mine", -1.0, source="roster")]
    wire = [make(10, "claimable", 2.0, source="waivers")]
    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY)
    assert any("claim" in w for w in report.swaps[0].warnings)


def test_illegal_swap_is_flagged_but_still_shown():
    """Dropping your only center should warn, not silently recommend."""
    roster = [
        make(1, "only center", -1.0, positions=("C",), source="roster"),
        make(2, "guard a", 1.0, source="roster"),
        make(3, "guard b", 1.0, source="roster"),
    ]
    slots = ["C", "G", "Util"]
    wire = [make(10, "good guard", 2.0, positions=("PG",))]

    report = recommend(roster, wire, slots, schedule=schedule_all(), today=TODAY)
    swap = report.swaps[0]
    assert swap.drop.name == "only center"
    assert swap.legal is False
    assert any("cannot legally fill" in w for w in swap.warnings)


def test_selling_low_is_flagged():
    roster = [make(1, "hot but worst", 0.0, source="roster", trend=1.5)]
    wire = [make(10, "better", 1.0)]
    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY)
    assert any("trending up" in w and "selling low" in w for w in report.swaps[0].warnings)


def test_tiny_sample_is_flagged():
    roster = [make(1, "mine", -1.0, source="roster")]
    hot = make(10, "one game wonder", 2.0, games=1.0)
    report = recommend(roster, hot and [hot], SLOTS, schedule=schedule_all(), today=TODAY)
    assert any("tiny sample" in w for w in report.swaps[0].warnings)


def test_schedule_context_appears_in_reasons():
    roster = [make(1, "mine", -1.0, source="roster")]
    wire = [make(10, "pickup", 2.0, team="BOS")]
    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY)
    assert any("next 7 days" in reason for reason in report.swaps[0].reasons)


def test_each_add_appears_at_most_once():
    roster = [make(i, f"mine{i}", -1.0, source="roster") for i in range(1, 4)]
    wire = [make(10, "pickup", 2.0)]
    report = recommend(roster, wire, SLOTS, schedule=schedule_all(), today=TODAY)
    assert len(report.swaps) == 1


def test_rostered_players_are_not_suggested_as_adds():
    mine = make(1, "mine", 2.0, source="roster")
    roster = [mine, make(2, "other", -1.0, source="roster")]
    report = recommend(roster, [mine], SLOTS, schedule=schedule_all(), today=TODAY)
    assert report.swaps == []


def test_format_delta_line_marks_turnover_gains():
    line = format_delta_line({"TO": 0.5, "PTS": 0.9, "AST": -0.3})
    assert "PTS +0.90" in line
    assert "fewer TO" in line
    assert "AST -0.30" in line


def test_format_delta_line_handles_no_change():
    assert format_delta_line({"PTS": 0.01}) == "no meaningful category change"
