"""NBA stat-id names that the SDK doesn't ship.

`yahoo_fantasy_api` builds its stat-id -> name map from the league's scoring
categories only, and its static fallback map covers MLB and NHL but not NBA.
So a 9-cat response comes back with FG%/FT%/PTS/REB/... but *without* games
played or the made/attempted splits -- and we need both: GP to turn Yahoo's
window totals into per-game rates, and FGM/FGA to value percentages by volume
rather than treating a 2-for-2 night like a 10-for-20 one.

Injecting this map makes those fields come back named. Run
`python -m fantasy probe --raw` against your league to confirm the ids, since
Yahoo has been known to shuffle rarely-used ones.
"""
from __future__ import annotations

from typing import Any

NBA_STAT_IDS: dict[int, str] = {
    0: "GP",
    1: "GS",
    2: "MIN",
    3: "FGA",
    4: "FGM",
    5: "FG%",
    6: "FTA",
    7: "FTM",
    8: "FT%",
    9: "3PTA",
    10: "3PTM",
    11: "3PT%",
    12: "PTS",
    13: "OREB",
    14: "DREB",
    15: "REB",
    16: "AST",
    17: "ST",
    18: "BLK",
    19: "TO",
    20: "A/T",
    21: "PF",
    22: "DISQ",
    23: "TECH",
    24: "EJCT",
    25: "FF",
    26: "MPG",
    27: "DD",
    28: "TD",
    # Composite "made/attempted" display stats; values arrive as "12/25".
    9004003: "FGM/A",
    9007006: "FTM/A",
}

# Composites we split into their parts when the discrete ids are absent.
RATIO_FIELDS = {"FGM/A": ("FGM", "FGA"), "FTM/A": ("FTM", "FTA")}


def install_stat_map(league: Any) -> dict[int, str]:
    """Merge the NBA ids into a league's stat map, keeping league names on top.

    The league's own `stat_categories` names win for scoring categories, so a
    league that calls steals "STL" instead of "ST" keeps its own label.
    """
    # Force the SDK to build its map first, then layer ours underneath.
    league._cache_stats_id_map("nba")
    existing = dict(league.stats_id_map or {})
    merged = dict(NBA_STAT_IDS)
    merged.update(existing)  # league settings take precedence
    league.stats_id_map = merged
    return merged
