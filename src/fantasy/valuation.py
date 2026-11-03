"""9-category valuation: per-window z-scores blended into one number.

Method, briefly:

* Counting cats (PTS, REB, AST, 3PTM, ST, BLK, TO) are scored as standard
  z-scores against the pool of rostered players -- the population you actually
  compete against, not every player in the database. Turnovers are negated.
* Percentage cats are scored by *volume-weighted impact*, not raw percentage.
  A player who shoots 2-for-2 has a raw 100% FG but barely moves a weekly
  matchup; someone at 10-for-16 moves it a lot. Impact is
  `FGM - FGA * pool_FG%`: makes above what an average-efficiency player would
  have produced on the same attempts. That value is then z-scored.
* Windows (last 7 days / last 30 / season) are scored independently, then
  blended with recency-tilted weights, so a player heating up or cooling off
  shows up in the headline number instead of being averaged away.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from .config import NEGATIVE_CATS, WindowWeights, canonical_cat
from .players import WINDOW_ORDER, Player

PCT_CATS = {"FG%", "FT%"}
PCT_PARTS = {"FG%": ("FGM", "FGA"), "FT%": ("FTM", "FTA")}

# A window only counts if the player actually played enough in it.
MIN_GAMES = {"last7": 1.0, "last30": 3.0, "season": 5.0}
# Pool membership is stricter -- fringe samples would inflate the spread.
POOL_MIN_GAMES = {"last7": 2.0, "last30": 5.0, "season": 8.0}


@dataclass
class CatStats:
    mean: float = 0.0
    sd: float = 1.0

    def z(self, value: float) -> float:
        if self.sd <= 0:
            return 0.0
        return (value - self.mean) / self.sd


@dataclass
class PoolModel:
    """Distribution of each category across the rostered-player pool."""

    window: str
    cats: list[str]
    stats: dict[str, CatStats] = field(default_factory=dict)
    pool_pct: dict[str, float] = field(default_factory=dict)  # 'FG%' -> .472
    size: int = 0

    def z_for(self, player: Player) -> dict[str, float]:
        window = player.window(self.window)
        if not window.usable or window.games < MIN_GAMES.get(self.window, 1.0):
            return {}
        out: dict[str, float] = {}
        for cat in self.cats:
            value = _cat_value(window.per_game, cat, self.pool_pct)
            if value is None:
                continue
            z = self.stats[cat].z(value) if cat in self.stats else 0.0
            if cat in NEGATIVE_CATS:
                z = -z
            out[cat] = z
        return out


def _cat_value(
    per_game: dict[str, float], cat: str, pool_pct: dict[str, float]
) -> float | None:
    """The quantity we z-score for a category.

    Counting cats use the per-game rate; percentage cats use volume-weighted
    impact against the pool's aggregate percentage.
    """
    cat = canonical_cat(cat)
    if cat in PCT_CATS:
        made_key, att_key = PCT_PARTS[cat]
        made = per_game.get(made_key)
        attempted = per_game.get(att_key)
        if made is None or attempted is None:
            return None
        baseline = pool_pct.get(cat)
        if baseline is None:
            return None
        return made - attempted * baseline
    return per_game.get(cat)


def _pool_percentages(players: Sequence[Player], window: str) -> dict[str, float]:
    """League-wide FG%/FT% across the pool, weighted by attempts."""
    out: dict[str, float] = {}
    for cat, (made_key, att_key) in PCT_PARTS.items():
        made = attempted = 0.0
        for player in players:
            per_game = player.window(window).per_game
            if made_key in per_game and att_key in per_game:
                made += per_game[made_key]
                attempted += per_game[att_key]
        out[cat] = made / attempted if attempted > 0 else 0.0
    return out


def build_pool(
    players: Iterable[Player], cats: Sequence[str], window: str
) -> PoolModel:
    """Fit the category distribution for one stat window."""
    cats = [canonical_cat(c) for c in cats]
    members = [
        p
        for p in players
        if p.window(window).usable
        and p.window(window).games >= POOL_MIN_GAMES.get(window, 1.0)
    ]
    model = PoolModel(window=window, cats=list(cats), size=len(members))
    if not members:
        return model

    model.pool_pct = _pool_percentages(members, window)
    for cat in cats:
        values = [
            v
            for v in (
                _cat_value(p.window(window).per_game, cat, model.pool_pct)
                for p in members
            )
            if v is not None
        ]
        if len(values) < 2:
            model.stats[cat] = CatStats(mean=0.0, sd=1.0)
            continue
        array = np.asarray(values, dtype=float)
        sd = float(array.std(ddof=1))
        model.stats[cat] = CatStats(
            mean=float(array.mean()), sd=sd if sd > 1e-9 else 1.0
        )
    return model


def category_weights(
    my_players: Sequence[Player],
    cats: Sequence[str],
    mode: str = "balanced",
    scale: float = 4.0,
    floor: float = 0.30,
) -> dict[str, float]:
    """Per-category weights used to turn z-scores into a single number.

    `balanced` weights every category equally -- the standard, build-agnostic
    view. `needs` tilts toward categories your roster can actually swing: a
    category you already dominate is close to locked, and one you're nowhere
    near is effectively punted; in both cases a marginal add changes fewer
    matchups than it would in a category you're near the middle of.

    This is a heuristic, not a projection. Weights are floored so no category
    is ever fully ignored.
    """
    cats = [canonical_cat(c) for c in cats]
    if mode == "balanced":
        return {cat: 1.0 for cat in cats}
    if mode != "needs":
        raise SystemExit(f"unknown weighting mode {mode!r} (use balanced|needs)")

    # Team strength per category: sum of the blended z of the players most
    # likely to be in your lineup.
    ranked = sorted(my_players, key=lambda p: -p.blended_value)[:10]
    weights: dict[str, float] = {}
    for cat in cats:
        team_z = sum(p.blended_z.get(cat, 0.0) for p in ranked)
        weight = math.exp(-((team_z / scale) ** 2) / 2.0)
        weights[cat] = max(floor, weight)
    return weights


def score_players(
    players: Iterable[Player],
    pools: dict[str, PoolModel],
    cats: Sequence[str],
    weights: WindowWeights,
    cat_weights: dict[str, float] | None = None,
) -> None:
    """Compute per-window z-scores, the blended value, and the trend delta.

    Window weights are renormalized per player over the windows that player
    actually has, so someone back from injury with no 30-day sample is still
    scored on what they have rather than penalized to zero.
    """
    cats = [canonical_cat(c) for c in cats]
    cat_weights = cat_weights or {cat: 1.0 for cat in cats}
    window_weights = weights.normalized()

    for player in players:
        player.z = {}
        player.value = {}
        for window_name in WINDOW_ORDER:
            pool = pools.get(window_name)
            if pool is None:
                continue
            z = pool.z_for(player)
            if not z:
                continue
            player.z[window_name] = z
            player.value[window_name] = sum(
                score * cat_weights.get(cat, 1.0) for cat, score in z.items()
            )

        available = {w: window_weights[w] for w in player.z if w in window_weights}
        total_weight = sum(available.values())
        if total_weight <= 0:
            player.blended_z = {}
            player.blended_value = 0.0
            player.trend = 0.0
            continue

        blended: dict[str, float] = {}
        for cat in cats:
            blended[cat] = sum(
                player.z[window].get(cat, 0.0) * weight / total_weight
                for window, weight in available.items()
            )
        player.blended_z = blended
        player.blended_value = sum(
            score * cat_weights.get(cat, 1.0) for cat, score in blended.items()
        )

        recent = player.value.get("last7", player.value.get("last30"))
        baseline = player.value.get("season", player.value.get("last30"))
        player.trend = (recent - baseline) if (recent is not None and baseline is not None) else 0.0


def category_deltas(add: Player, drop: Player) -> dict[str, float]:
    """Per-category z swing from adding one player and dropping another."""
    cats = set(add.blended_z) | set(drop.blended_z)
    return {
        cat: add.blended_z.get(cat, 0.0) - drop.blended_z.get(cat, 0.0)
        for cat in cats
    }


def team_category_profile(
    players: Sequence[Player], cats: Sequence[str], top_n: int = 10
) -> dict[str, float]:
    """Summed blended z per category for your likely starters."""
    ranked = sorted(players, key=lambda p: -p.blended_value)[:top_n]
    return {
        canonical_cat(cat): sum(p.blended_z.get(canonical_cat(cat), 0.0) for p in ranked)
        for cat in cats
    }
