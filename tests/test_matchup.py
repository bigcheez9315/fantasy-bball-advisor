"""Matchup parsing and projection. Payload values are synthetic; only the
nesting mirrors Yahoo's responses."""
import datetime as dt

from fantasy.matchup import (
    Projection,
    best_per_add,
    outlook,
    parse_matchup,
    parse_transactions,
    WeekMove,
)
from fantasy.players import Player

ME, OPP = "478.l.1.t.6", "478.l.1.t.1"


def _team(key, name, fg, pts, to, adds, remaining):
    return {
        "team": [
            [{"team_key": key}, {"name": name}, [], {"roster_adds": {"value": str(adds)}}],
            {
                "team_stats": {
                    "stats": [
                        {"stat": {"stat_id": "9004003", "value": fg}},
                        {"stat": {"stat_id": "5", "value": ".500"}},
                        {"stat": {"stat_id": "12", "value": str(pts)}},
                        {"stat": {"stat_id": "19", "value": str(to)}},
                    ]
                },
                "team_remaining_games": {"total": {"remaining_games": remaining}},
            },
        ]
    }


def _matchup_payload():
    return {
        "fantasy_content": {
            "team": [
                [{"team_key": ME}],
                {
                    "matchups": {
                        "0": {
                            "matchup": {
                                "week": "5",
                                "week_start": "2026-11-16",
                                "week_end": "2026-11-22",
                                "stat_winners": [
                                    {"stat_winner": {"stat_id": "12", "winner_team_key": ME}}
                                ],
                                "0": {
                                    "teams": {
                                        "0": _team(ME, "Mine", "50/100", 160, 20, 4, 30),
                                        "1": _team(OPP, "Theirs", "40/100", 120, 10, 1, 40),
                                        "count": 2,
                                    }
                                },
                            }
                        },
                        "count": 1,
                    }
                },
            ]
        }
    }


def test_parse_matchup_sides_and_splits():
    m = parse_matchup(_matchup_payload(), ME)
    assert m.week == 5 and m.end == dt.date(2026, 11, 22)
    assert m.me.name == "Mine" and m.opp.name == "Theirs"
    assert (m.me.totals["FGM"], m.me.totals["FGA"]) == (50.0, 100.0)
    assert m.me.adds_used == 4 and m.opp.adds_used == 1
    assert m.opp.remaining_games == 40
    assert m.leaders["PTS"] == ME


def _tx(ts, kind, name, data):
    return {
        "transaction": [
            {"type": kind, "status": "successful", "timestamp": str(ts)},
            {
                "players": {
                    "0": {
                        "player": [
                            [{"player_id": "1"}, {"name": {"full": name}},
                             {"editorial_team_abbr": "LAC"}],
                            {"transaction_data": data},
                        ]
                    },
                    "count": 1,
                }
            },
        ]
    }


def test_parse_transactions_filters_week_and_team():
    monday_noon = int(dt.datetime(2026, 11, 16, 17, tzinfo=dt.timezone.utc).timestamp())
    last_week = monday_noon - 3 * 86400
    payload = {
        "fantasy_content": {
            "league": [
                {},
                {
                    "transactions": {
                        "0": _tx(monday_noon, "add", "New Guy",
                                 [{"type": "add", "source_type": "freeagents",
                                   "destination_team_key": ME}]),
                        "1": _tx(monday_noon, "drop", "Not Mine",
                                 {"type": "drop", "source_type": "team",
                                  "source_team_key": OPP}),
                        "2": _tx(last_week, "drop", "Old Move",
                                 {"type": "drop", "source_type": "team",
                                  "source_team_key": ME}),
                        "count": 3,
                    }
                },
            ]
        }
    }
    moves = parse_transactions(payload, ME, dt.date(2026, 11, 16))
    assert [(m.kind, m.player) for m in moves] == [("add", "New Guy")]


def test_outlook_turnovers_are_inverted_and_final_week_is_certain():
    me = Projection(totals={"PTS": 100, "TO": 10}, added={})
    opp = Projection(totals={"PTS": 90, "TO": 20}, added={})
    rows = {r.cat: r for r in outlook(["PTS", "TO"], {}, {}, me, opp)}
    assert rows["PTS"].p_win == 1.0
    assert rows["TO"].p_win == 1.0  # fewer turnovers wins


def test_keeper_safe_filter_skips_costly_drops():
    a, b = Player(pid=1, name="a"), Player(pid=2, name="b")
    day = dt.date(2026, 11, 17)
    costly = WeekMove(a, b, delta_wins=0.4, season_delta=-3.0, effective=day, swings={})
    safe = WeekMove(a, b, delta_wins=0.3, season_delta=+1.0, effective=day, swings={})
    assert best_per_add([costly, safe], keeper_safe=True) == [safe]
    assert best_per_add([costly, safe], keeper_safe=False) == [costly]
    assert best_per_add([costly, safe]) == [costly]
