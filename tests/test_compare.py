from fantasy.compare import per_game


def test_per_game_uses_raw_totals_and_derives_percentages():
    rates = per_game({"MIN": 81, "FGA": 30, "FGM": 12, "3PTA": 10, "3PTM": 4, "PTS": 33}, 3)
    assert rates["MIN"] == 27 and rates["FGA"] == 10
    assert rates["FG%"] == 0.4 and rates["3PT%"] == 0.4
    assert per_game({"MIN": 10}, 0) == {}
