"""Orchestration: fetch everything once, value it, and answer the questions.

One `run()` produces the whole picture -- roster, wire, valuations, waiver
suggestions, today's lineup -- so a single command gives you everything worth
discussing instead of making you stitch together four of them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import lineup as lineup_mod
from . import schedule as schedule_mod
from . import waivers as waivers_mod
from .client import FantasyClient
from .config import WindowWeights, canonical_cat
from .players import WINDOW_ORDER, Player, attach_stats, parse_row
from .valuation import (
    PoolModel,
    build_pool,
    category_weights,
    score_players,
    team_category_profile,
)


@dataclass
class Analysis:
    league_name: str = ""
    league_key: str = ""
    team_name: str = ""
    cats: list[str] = field(default_factory=list)
    slots: list[str] = field(default_factory=list)
    date: dt.date = field(default_factory=dt.date.today)
    mode: str = "balanced"
    weights: WindowWeights = field(default_factory=WindowWeights)
    cat_weights: dict[str, float] = field(default_factory=dict)

    my_players: list[Player] = field(default_factory=list)
    available: list[Player] = field(default_factory=list)
    pool_size: int = 0
    pools: dict[str, PoolModel] = field(default_factory=dict)
    schedule: schedule_mod.Schedule = field(default_factory=schedule_mod.Schedule)

    waivers: waivers_mod.WaiverReport = field(default_factory=waivers_mod.WaiverReport)
    lineup: lineup_mod.LineupPlan | None = None
    team_profile: dict[str, float] = field(default_factory=dict)
    api_calls: int = 0
    notes: list[str] = field(default_factory=list)

    def my_sorted(self) -> list[Player]:
        return sorted(self.my_players, key=lambda p: -p.blended_value)


def _shortlist(
    available: list[Player], limit: int, min_owned: float = 8.0
) -> list[Player]:
    """Trim the wire to a plausible candidate set before fetching stats.

    An NBA league's available pool runs to several hundred players, and pulling
    three stat windows for all of them is both slow and pointless -- the bottom
    is deep-league filler. Ownership percentage is Yahoo's own crowd signal, so
    it's a good cheap prior; anything reasonably owned is kept regardless of
    where it lands in the ordering.
    """
    ranked = sorted(available, key=lambda p: -p.pct_owned)
    keep = {p.pid: p for p in ranked[:limit]}
    for player in ranked:
        if player.pct_owned >= min_owned:
            keep.setdefault(player.pid, player)
    return list(keep.values())


def run(
    client: FantasyClient,
    date: dt.date | None = None,
    mode: str = "balanced",
    weights: WindowWeights | None = None,
    candidate_limit: int = 60,
    min_gain: float = waivers_mod.DEFAULT_MIN_GAIN,
    include_questionable: bool = True,
    skip_schedule: bool = False,
) -> Analysis:
    date = date or dt.date.today()
    weights = weights or WindowWeights()
    analysis = Analysis(date=date, mode=mode, weights=weights)

    settings = client.settings()
    analysis.league_name = settings.get("name", "")
    analysis.league_key = client.league_key or ""
    analysis.cats = [canonical_cat(c) for c in client.categories()]
    analysis.slots = client.roster_slots()

    # --- fetch -------------------------------------------------------------
    roster_rows = client.my_roster()
    my_players = [parse_row(r, "roster") for r in roster_rows]

    taken_rows = client.taken_players()
    roster_ids = {p.pid for p in my_players}
    pool_players = [
        parse_row(r, "taken") for r in taken_rows if int(r["player_id"]) not in roster_ids
    ]

    available_rows = client.available_players("A")
    waiver_ids: set[int] = set()
    try:
        waiver_ids = {int(r["player_id"]) for r in client.available_players("W")}
    except Exception as exc:  # non-fatal; we just lose the claim/free-add tag
        analysis.notes.append(f"Could not separate waivers from free agents: {exc}")

    available = [
        parse_row(r, "waivers" if int(r["player_id"]) in waiver_ids else "fa")
        for r in available_rows
        if int(r["player_id"]) not in roster_ids
    ]
    candidates = _shortlist(available, candidate_limit)

    # --- stats -------------------------------------------------------------
    everyone: dict[int, Player] = {}
    for group in (my_players, pool_players, candidates):
        for player in group:
            everyone.setdefault(player.pid, player)

    stats_by_window: dict[str, dict[int, dict]] = {}
    for window in WINDOW_ORDER:
        stats_by_window[window] = client.stats(everyone.keys(), window)
    attach_stats(everyone, stats_by_window, analysis.cats)

    # --- value -------------------------------------------------------------
    # The pool is every rostered player league-wide: the population you
    # actually compete against, which is what makes a z-score meaningful.
    rostered = my_players + pool_players
    analysis.pools = {
        window: build_pool(rostered, analysis.cats, window) for window in WINDOW_ORDER
    }
    analysis.pool_size = max((p.size for p in analysis.pools.values()), default=0)

    all_players = list(everyone.values())
    score_players(all_players, analysis.pools, analysis.cats, weights)

    if mode == "needs":
        # Needs weighting depends on roster strength, which depends on a first
        # balanced pass -- hence scoring twice.
        analysis.cat_weights = category_weights(my_players, analysis.cats, "needs")
        score_players(
            all_players, analysis.pools, analysis.cats, weights, analysis.cat_weights
        )
    else:
        analysis.cat_weights = category_weights(my_players, analysis.cats, "balanced")

    analysis.my_players = my_players
    analysis.available = candidates
    analysis.team_profile = team_category_profile(my_players, analysis.cats)

    # --- advise ------------------------------------------------------------
    analysis.schedule = (
        schedule_mod.Schedule(available=False, error="schedule lookup skipped")
        if skip_schedule
        else schedule_mod.load(start=date, days=14)
    )
    if analysis.schedule.error:
        analysis.notes.append(analysis.schedule.error)

    analysis.waivers = waivers_mod.recommend(
        my_players,
        candidates,
        analysis.slots,
        schedule=analysis.schedule,
        today=date,
        min_gain=min_gain,
    )
    analysis.lineup = lineup_mod.optimize(
        my_players,
        analysis.slots,
        date=date,
        schedule=analysis.schedule,
        include_questionable=include_questionable,
    )

    if len(analysis.slots) >= len(my_players):
        analysis.notes.append(
            "Your roster is no larger than your startable slots, so there is no "
            "start/sit decision today."
        )
    analysis.api_calls = client.api_calls
    return analysis
