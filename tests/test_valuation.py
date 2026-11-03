"""Valuation math, checked against hand-computed values."""
from __future__ import annotations

import math

import pytest

from fantasy.config import NINE_CAT, WindowWeights
from fantasy.players import Player, Window, parse_window
from fantasy.valuation import build_pool, category_deltas, score_players

CATS = NINE_CAT


def make_player(pid: int, name: str, per_game: dict, games: float = 10.0, **kw) -> Player:
    player = Player(pid=pid, name=name, **kw)
    player.windows = {
        window: Window(games=games, per_game=dict(per_game))
        for window in ("last7", "last30", "season")
    }
    return player


def test_counting_cat_z_is_standard_score():
    # PTS of 10, 20, 30 -> mean 20, sample sd 10.
    pool = [
        make_player(i, f"p{i}", {"PTS": pts, "FGM": 5, "FGA": 10, "FTM": 2, "FTA": 2})
        for i, pts in enumerate([10.0, 20.0, 30.0], start=1)
    ]
    model = build_pool(pool, ["PTS"], "season")
    assert model.stats["PTS"].mean == pytest.approx(20.0)
    assert model.stats["PTS"].sd == pytest.approx(10.0)

    z = model.z_for(pool[2])  # the 30 PPG player
    assert z["PTS"] == pytest.approx(1.0)
    assert model.z_for(pool[0])["PTS"] == pytest.approx(-1.0)


def test_turnovers_are_inverted():
    pool = [
        make_player(i, f"p{i}", {"TO": to, "FGM": 5, "FGA": 10, "FTM": 2, "FTA": 2})
        for i, to in enumerate([1.0, 2.0, 3.0], start=1)
    ]
    model = build_pool(pool, ["TO"], "season")
    # Fewest turnovers should score best.
    assert model.z_for(pool[0])["TO"] > 0
    assert model.z_for(pool[2])["TO"] < 0


def test_fg_pct_is_volume_weighted_not_raw_percentage():
    """A 2-for-2 night must not outrank efficient high volume."""
    sniper = make_player(1, "low volume", {"FGM": 2.0, "FGA": 2.0})  # 100% on 2 shots
    workhorse = make_player(2, "high volume", {"FGM": 10.0, "FGA": 16.0})  # 62.5% on 16
    filler = [
        make_player(10 + i, f"avg{i}", {"FGM": 5.0, "FGA": 11.0}) for i in range(8)
    ]

    model = build_pool([sniper, workhorse, *filler], ["FG%"], "season")
    # Pool shoots (2+10+40) / (2+16+88) = 52/106
    assert model.pool_pct["FG%"] == pytest.approx(52 / 106)

    sniper_z = model.z_for(sniper)["FG%"]
    workhorse_z = model.z_for(workhorse)["FG%"]
    assert workhorse_z > sniper_z, "volume must beat a tiny perfect sample"

    # Impact = FGM - FGA * pool_pct, then z-scored.
    baseline = 52 / 106
    expected_impact = 10.0 - 16.0 * baseline
    raw = [p.window("season").per_game for p in [sniper, workhorse, *filler]]
    impacts = [pg["FGM"] - pg["FGA"] * baseline for pg in raw]
    mean = sum(impacts) / len(impacts)
    variance = sum((i - mean) ** 2 for i in impacts) / (len(impacts) - 1)
    assert workhorse_z == pytest.approx((expected_impact - mean) / math.sqrt(variance))


def test_pct_cats_unscored_without_attempt_data():
    """No FGM/FGA means FG% is skipped rather than silently treated as zero."""
    pool = [make_player(i, f"p{i}", {"PTS": 10.0 * i}) for i in range(1, 5)]
    model = build_pool(pool, ["FG%", "PTS"], "season")
    z = model.z_for(pool[0])
    assert "PTS" in z
    assert "FG%" not in z


def test_window_weights_renormalize_when_a_window_is_missing():
    pool = [
        make_player(i, f"p{i}", {"PTS": pts, "FGM": 5, "FGA": 10})
        for i, pts in enumerate([10.0, 20.0, 30.0], start=1)
    ]
    pools = {w: build_pool(pool, ["PTS"], w) for w in ("last7", "last30", "season")}

    # A player with only a season sample (just back from injury).
    returnee = make_player(99, "returnee", {"PTS": 30.0, "FGM": 5, "FGA": 10})
    returnee.windows.pop("last7")
    returnee.windows.pop("last30")

    score_players([returnee], pools, ["PTS"], WindowWeights())
    # Should be scored on season alone (+1.0), not diluted toward zero.
    assert returnee.blended_value == pytest.approx(1.0)


def test_trend_compares_recent_to_season():
    pool = [
        make_player(i, f"p{i}", {"PTS": pts, "FGM": 5, "FGA": 10})
        for i, pts in enumerate([10.0, 20.0, 30.0], start=1)
    ]
    pools = {w: build_pool(pool, ["PTS"], w) for w in ("last7", "last30", "season")}

    riser = make_player(50, "riser", {"PTS": 20.0, "FGM": 5, "FGA": 10})
    riser.windows["last7"] = Window(games=4, per_game={"PTS": 30.0, "FGM": 5, "FGA": 10})
    score_players([riser], pools, ["PTS"], WindowWeights())
    assert riser.trend == pytest.approx(1.0)  # +1 z in L7 vs 0 z on the season


def test_min_games_filter_excludes_thin_windows():
    pool = [
        make_player(i, f"p{i}", {"PTS": pts, "FGM": 5, "FGA": 10}, games=20)
        for i, pts in enumerate([10.0, 20.0, 30.0], start=1)
    ]
    model = build_pool(pool, ["PTS"], "season")
    thin = make_player(77, "thin", {"PTS": 40.0, "FGM": 5, "FGA": 10}, games=2)
    assert model.z_for(thin) == {}  # season needs 5+ games


def test_category_deltas_are_add_minus_drop():
    add = Player(pid=1, name="add")
    add.blended_z = {"PTS": 1.0, "AST": -0.5}
    drop = Player(pid=2, name="drop")
    drop.blended_z = {"PTS": 0.25, "REB": 0.75}
    deltas = category_deltas(add, drop)
    assert deltas["PTS"] == pytest.approx(0.75)
    assert deltas["AST"] == pytest.approx(-0.5)
    assert deltas["REB"] == pytest.approx(-0.75)


def test_parse_window_divides_totals_by_games_played():
    raw = {"GP": 10.0, "PTS": 200.0, "REB": 50.0, "FGM/A": "80/160", "FG%": 0.5}
    window = parse_window(raw, ["PTS", "REB", "FG%"])
    assert window.games == 10.0
    assert window.per_game["PTS"] == pytest.approx(20.0)
    assert window.per_game["REB"] == pytest.approx(5.0)
    # Composite split, then per-game.
    assert window.per_game["FGM"] == pytest.approx(8.0)
    assert window.per_game["FGA"] == pytest.approx(16.0)
    # Rates stay as reported, not divided.
    assert window.per_game["FG%"] == pytest.approx(0.5)


def test_parse_window_without_games_is_unusable():
    assert not parse_window({"PTS": 100.0}, ["PTS"]).usable
    assert not parse_window({}, ["PTS"]).usable
