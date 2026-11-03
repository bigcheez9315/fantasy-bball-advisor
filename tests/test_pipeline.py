"""End-to-end run against a fake Yahoo client.

Exercises the whole path -- fetch, parse, value, recommend, render -- with
Yahoo-shaped payloads, so the plumbing and all three output formats are
covered without network access or credentials.
"""
from __future__ import annotations

import datetime as dt
import json
import random

import pytest

from fantasy import analysis as analysis_mod
from fantasy import report as report_mod
from fantasy.config import WindowWeights
from fantasy.schedule import Schedule

TODAY = dt.date(2026, 11, 4)
CATS = ["FG%", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]
SLOTS = ["PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util"]
POSITION_SETS = [
    ["PG", "G"], ["SG", "G"], ["PG", "SG", "G"], ["SF", "F"],
    ["PF", "F"], ["SF", "PF", "F"], ["C"], ["PF", "C", "F"],
]
TEAMS = ["BOS", "LAL", "GSW", "PHO", "MIL", "DEN", "NYK", "MIA"]


class FakeClient:
    """Stands in for FantasyClient, returning rows shaped like the SDK's."""

    def __init__(self, seed: int = 7, roster_size: int = 13, teams: int = 12,
                 wire: int = 90):
        self.rng = random.Random(seed)
        self.api_calls = 0
        self.league_key = "466.l.99999"
        self._skill: dict[int, float] = {}

        self._roster = [self._player(pid) for pid in range(1, roster_size + 1)]
        start = roster_size + 1
        total_taken = roster_size * teams
        self._other_taken = [
            self._player(pid) for pid in range(start, start + total_taken - roster_size)
        ]
        wire_start = start + total_taken
        self._wire = [self._player(pid) for pid in range(wire_start, wire_start + wire)]
        # The last handful of wire players sit on waivers rather than free agency.
        self._waiver_ids = {p["player_id"] for p in self._wire[:5]}

    def _player(self, pid: int) -> dict:
        self._skill[pid] = self.rng.gauss(0, 1)
        status = ""
        roll = self.rng.random()
        if roll < 0.06:
            status = "O"
        elif roll < 0.14:
            status = "GTD"
        return {
            "player_id": pid,
            "name": f"Player {pid:03d}",
            "position_type": "P",
            "eligible_positions": list(self.rng.choice(POSITION_SETS)),
            "status": status,
            "editorial_team_abbr": self.rng.choice(TEAMS),
            "percent_owned": max(0.0, min(100.0, self.rng.gauss(35, 30))),
        }

    # -- interface used by analysis.run ---------------------------------

    def settings(self) -> dict:
        return {"name": "Test Hoops League", "num_teams": 12, "season": "2026"}

    def categories(self) -> list[str]:
        return list(CATS)

    def roster_slots(self) -> list[str]:
        return list(SLOTS)

    def my_team_key(self) -> str:
        return "466.l.99999.t.3"

    def my_roster(self, day=None) -> list[dict]:
        return [dict(p, selected_position="Util") for p in self._roster]

    def taken_players(self) -> list[dict]:
        return [dict(p) for p in self._roster + self._other_taken]

    def available_players(self, status: str = "A") -> list[dict]:
        if status == "W":
            return [dict(p) for p in self._wire if p["player_id"] in self._waiver_ids]
        return [dict(p) for p in self._wire]

    def stats(self, player_ids, window: str) -> dict[int, dict]:
        """Yahoo-shaped totals: counting stats plus GP and made/attempt composites."""
        games = {"last7": 3, "last30": 12, "season": 20}[window]
        # Recent windows wobble around the player's true skill.
        noise = {"last7": 0.9, "last30": 0.35, "season": 0.0}[window]
        out = {}
        for pid in player_ids:
            pid = int(pid)
            skill = self._skill.get(pid, 0.0) + self.rng.gauss(0, noise)
            minutes = max(6.0, 24 + skill * 6)
            fga = max(2.0, 11 + skill * 3)
            fgm = fga * min(0.68, max(0.34, 0.46 + skill * 0.02))
            fta = max(0.4, 3 + skill * 1.2)
            ftm = fta * min(0.95, max(0.5, 0.78 + skill * 0.02))
            out[pid] = {
                "player_id": pid,
                "name": f"Player {pid:03d}",
                "GP": float(games),
                "MIN": minutes * games,
                "PTS": (fgm * 2 + ftm + max(0.0, 1.5 + skill)) * games,
                "REB": max(0.5, 4 + skill * 2) * games,
                "AST": max(0.3, 3 + skill * 1.8) * games,
                "ST": max(0.1, 0.8 + skill * 0.4) * games,
                "BLK": max(0.0, 0.5 + skill * 0.35) * games,
                "3PTM": max(0.0, 1.2 + skill * 0.8) * games,
                "TO": max(0.3, 1.6 + skill * 0.5) * games,
                "FGM/A": f"{fgm * games:.0f}/{fga * games:.0f}",
                "FTM/A": f"{ftm * games:.0f}/{fta * games:.0f}",
                "FG%": round(fgm / fga, 3),
                "FT%": round(ftm / fta, 3),
            }
        return out


@pytest.fixture
def result(monkeypatch):
    # Deterministic schedule: every team plays most days.
    schedule = Schedule(
        by_date={
            (TODAY + dt.timedelta(days=d)).isoformat(): set(TEAMS[: 6 if d % 2 else 8])
            for d in range(14)
        }
    )
    monkeypatch.setattr(analysis_mod.schedule_mod, "load", lambda **kw: schedule)
    return analysis_mod.run(FakeClient(), date=TODAY, candidate_limit=40)


def test_pipeline_produces_a_valued_roster(result):
    assert result.league_name == "Test Hoops League"
    assert result.cats == CATS
    assert len(result.my_players) == 13
    assert result.pool_size > 100  # the rostered pool, not just my team

    # Everyone healthy enough got scored across all three windows.
    scored = [p for p in result.my_players if p.blended_value]
    assert len(scored) == 13
    for player in scored:
        assert set(player.value) == {"last7", "last30", "season"}
        # All nine categories valued, percentages included.
        assert set(player.blended_z) == set(CATS)


def test_pipeline_lineup_is_legal_and_full(result):
    plan = result.lineup
    assert plan is not None
    # 13 players, 10 slots, some out/no-game: expect a nearly full lineup.
    assert len(plan.starters) >= 8
    assert len(plan.starters) <= len(SLOTS)

    # No one is started twice, and nobody ruled out is started.
    started = [p.pid for _, p in plan.starters]
    assert len(started) == len(set(started))
    assert all(not p.is_out for _, p in plan.starters)

    # Every assignment respects position eligibility.
    from fantasy.lineup import eligible

    assert all(eligible(player, slot) for slot, player in plan.starters)


def test_pipeline_waiver_suggestions_are_coherent(result):
    report = result.waivers
    roster_ids = {p.pid for p in result.my_players}

    for swap in report.swaps:
        assert swap.net > 0
        assert swap.add.pid not in roster_ids
        assert swap.drop.pid in roster_ids
        assert not swap.add.is_out
        # The claimed net must match the underlying values.
        assert swap.net == pytest.approx(
            swap.add.blended_value - swap.drop.blended_value
        )
        # Categories should roughly account for the net gain.
        assert sum(swap.deltas.values()) == pytest.approx(swap.net, abs=1e-6)

    # Suggestions are ordered best-first.
    nets = [s.net for s in report.swaps]
    assert nets == sorted(nets, reverse=True)


def test_needs_mode_changes_the_weighting(monkeypatch):
    schedule = Schedule(by_date={TODAY.isoformat(): set(TEAMS)})
    monkeypatch.setattr(analysis_mod.schedule_mod, "load", lambda **kw: schedule)

    balanced = analysis_mod.run(FakeClient(), date=TODAY, mode="balanced",
                                candidate_limit=30)
    needs = analysis_mod.run(FakeClient(), date=TODAY, mode="needs",
                             candidate_limit=30)

    assert set(balanced.cat_weights.values()) == {1.0}
    assert any(w < 1.0 for w in needs.cat_weights.values())
    assert all(w >= 0.3 for w in needs.cat_weights.values())  # floored


def test_custom_window_weights_shift_valuations(monkeypatch):
    schedule = Schedule(by_date={TODAY.isoformat(): set(TEAMS)})
    monkeypatch.setattr(analysis_mod.schedule_mod, "load", lambda **kw: schedule)

    season_only = analysis_mod.run(
        FakeClient(), date=TODAY, weights=WindowWeights(0, 0, 1), candidate_limit=20
    )
    for player in season_only.my_players:
        if player.value.get("season") is not None:
            assert player.blended_value == pytest.approx(player.value["season"])


def test_all_three_output_formats_render(result, capsys):
    report_mod.print_full(result)
    terminal = capsys.readouterr().out
    assert "Test Hoops League" in terminal
    assert "Add / drop suggestions" in terminal
    assert "Your roster" in terminal

    payload = report_mod.to_dict(result)
    # Must be JSON-serializable with no leftover objects.
    encoded = json.dumps(payload)
    assert len(encoded) > 500
    assert payload["league"]["categories"] == CATS
    assert len(payload["roster"]) == 13
    assert payload["lineup"]["starters"]

    markdown = report_mod.to_markdown(result)
    assert markdown.startswith("# Waiver report - 2026-11-04")
    assert "## Roster" in markdown
    assert "## Discussion" in markdown
    assert "nan" not in markdown.lower()


def test_markdown_is_saved_with_a_dated_name(result, tmp_path):
    first = report_mod.save_markdown(result, tmp_path)
    assert first.name == "2026-11-04-waivers.md"
    # A second run the same day must not clobber the first.
    second = report_mod.save_markdown(result, tmp_path)
    assert second.name == "2026-11-04-waivers-1.md"
    assert first.read_text(encoding="utf-8")


def test_empty_wire_is_handled(monkeypatch):
    schedule = Schedule(by_date={TODAY.isoformat(): set(TEAMS)})
    monkeypatch.setattr(analysis_mod.schedule_mod, "load", lambda **kw: schedule)

    client = FakeClient(wire=0)
    result = analysis_mod.run(client, date=TODAY)
    assert result.waivers.swaps == []
    assert result.lineup is not None  # lineup advice still works
    report_mod.to_markdown(result)  # must not raise
