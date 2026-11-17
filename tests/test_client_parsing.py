"""Pin the real Yahoo payload shapes that the fake pipeline client hides.

Values here are synthetic; only the nesting mirrors Yahoo's responses.
"""
from fantasy.client import parse_players_stats, slots_from_positions
from fantasy.players import parse_window


def _stats_payload(pid: int, stats: dict[int, str]) -> dict:
    return {
        "fantasy_content": {
            "players": {
                "0": {
                    "player": [
                        [
                            {"player_key": f"478.p.{pid}"},
                            {"player_id": str(pid)},
                            {"name": {"full": "Test Guard"}},
                            [],
                        ],
                        {
                            "player_stats": {
                                "coverage_type": "lastweek",
                                "stats": [
                                    {"stat": {"stat_id": str(k), "value": v}}
                                    for k, v in stats.items()
                                ],
                            }
                        },
                    ]
                },
                "count": 1,
            }
        }
    }


def test_game_level_stats_carry_games_played():
    payload = _stats_payload(
        101,
        {0: "4", 3: "40", 4: "20", 5: ".500", 6: "10", 7: "8", 8: ".800",
         10: "6", 12: "60", 15: "20", 16: "16", 17: "4", 18: "2", 19: "8",
         20: "-"},
    )
    (row,) = parse_players_stats(payload)
    assert row["player_id"] == 101
    assert row["name"] == "Test Guard"
    assert row["GP"] == 4.0
    assert (row["FGM"], row["FGA"]) == (20.0, 40.0)
    assert "A/T" not in row  # non-numeric value dropped, not crashed on

    window = parse_window(row, ["FG%", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"])
    assert window.usable
    assert window.per_game["PTS"] == 15.0
    assert window.per_game["FGA"] == 10.0


def test_slots_come_from_positions_not_settings():
    positions = {
        "PG": {"position_type": "P", "count": 1, "is_starting_position": 1},
        "C": {"position_type": "P", "count": 2, "is_starting_position": 1},
        "Util": {"position_type": "P", "count": 2, "is_starting_position": 1},
        "BN": {"count": 3, "is_starting_position": 0},
        "IL": {"count": 5, "is_starting_position": 0},
    }
    assert slots_from_positions(positions) == ["PG", "C", "C", "Util", "Util"]
