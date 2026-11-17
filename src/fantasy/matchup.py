"""This week's head-to-head: where each category stands, where it's headed, and
which add/drops move the most categories *this week*.

Projection = Yahoo's live week-to-date totals + for each remaining day, the
optimized lineup's per-game production (both teams). Each category's margin is
turned into a win probability with a rough game-to-game variance model, and a
move is scored by the change in expected categories won. Heuristic, not a
simulator -- but it answers the right question: not "who is the better player"
but "which categories can still flip before Sunday".
"""
from __future__ import annotations

import copy
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from . import lineup as lineup_mod
from .config import NEGATIVE_CATS, canonical_cat, punt_cats, returning_players
from .players import Player
from .schedule import Schedule
from .statmap import NBA_STAT_IDS

PCT_PARTS = {"FG%": ("FGM", "FGA"), "FT%": ("FTM", "FTA")}
# Game-to-game spread relative to a Poisson process; NBA box stats are
# overdispersed (a 20 ppg scorer's nightly sd is ~7, Poisson says ~4.5).
OVERDISPERSION = 1.5
# Waiver claims in this league clear after `waiver_time` days.
DEFAULT_WAIVER_DAYS = 2
EASTERN = dt.timezone(dt.timedelta(hours=-5))  # Yahoo's fantasy day is ET


# -- parsing ------------------------------------------------------------------


def _merge_meta(parts: list) -> dict:
    out: dict[str, Any] = {}
    for item in parts:
        if isinstance(item, dict):
            out.update(item)
    return out


def _team_stats(stats: list[dict]) -> dict[str, float]:
    """Week-to-date team totals, with FGM/FGA and FTM/FTA split out."""
    out: dict[str, float] = {}
    for entry in stats:
        stat = entry.get("stat", {})
        name = NBA_STAT_IDS.get(int(stat.get("stat_id", -1)))
        value = stat.get("value")
        if name in ("FGM/A", "FTM/A"):
            made_key, att_key = ("FGM", "FGA") if name == "FGM/A" else ("FTM", "FTA")
            made, _, att = str(value or "").partition("/")
            try:
                out[made_key], out[att_key] = float(made or 0), float(att or 0)
            except ValueError:
                pass
        elif name:
            try:
                out[name] = float(value)
            except (TypeError, ValueError):
                pass
    return out


@dataclass
class Side:
    team_key: str
    name: str
    totals: dict[str, float]
    remaining_games: int = 0
    adds_used: int = 0


@dataclass
class Matchup:
    week: int
    start: dt.date
    end: dt.date
    me: Side
    opp: Side
    leaders: dict[str, str] = field(default_factory=dict)  # cat -> team_key


def parse_matchup(payload: dict, my_team_key: str) -> Matchup | None:
    team = payload["fantasy_content"]["team"]
    matchups = team[1]["matchups"]
    if not matchups or "0" not in matchups:
        return None
    m = matchups["0"]["matchup"]
    sides: list[Side] = []
    for key, entry in m["0"]["teams"].items():
        if key == "count":
            continue
        meta = _merge_meta(entry["team"][0])
        body = entry["team"][1]
        remaining = (body.get("team_remaining_games") or {}).get("total") or {}
        sides.append(
            Side(
                team_key=meta["team_key"],
                name=meta.get("name", meta["team_key"]),
                totals=_team_stats(body["team_stats"]["stats"]),
                remaining_games=int(remaining.get("remaining_games", 0) or 0),
                adds_used=int((meta.get("roster_adds") or {}).get("value", 0) or 0),
            )
        )
    me = next(s for s in sides if s.team_key == my_team_key)
    opp = next(s for s in sides if s.team_key != my_team_key)
    leaders = {}
    for winner in m.get("stat_winners", []):
        w = winner.get("stat_winner", {})
        cat = NBA_STAT_IDS.get(int(w.get("stat_id", -1)))
        if cat and w.get("winner_team_key"):
            leaders[cat] = w["winner_team_key"]
    return Matchup(
        week=int(m["week"]),
        start=dt.date.fromisoformat(m["week_start"]),
        end=dt.date.fromisoformat(m["week_end"]),
        me=me,
        opp=opp,
        leaders=leaders,
    )


@dataclass
class Move:
    when: dt.datetime
    kind: str  # add | drop
    player: str
    team: str
    source: str  # freeagents | waivers | team


def parse_transactions(payload: dict, team_key: str, since: dt.date) -> list[Move]:
    """This team's adds and drops since `since` (ET), newest first."""
    league = payload["fantasy_content"]["league"]
    txs = league[1]["transactions"] if len(league) > 1 else {}
    cutoff = dt.datetime.combine(since, dt.time(), EASTERN)
    moves: list[Move] = []
    for key, entry in (txs or {}).items():
        if key == "count":
            continue
        header, body = entry["transaction"][0], entry["transaction"][1]
        if header.get("status") != "successful":
            continue
        when = dt.datetime.fromtimestamp(int(header["timestamp"]), EASTERN)
        if when < cutoff:
            continue
        for pkey, pentry in body.get("players", {}).items():
            if pkey == "count":
                continue
            meta = _merge_meta(pentry["player"][0])
            data = pentry["player"][1]["transaction_data"]
            data = data[0] if isinstance(data, list) else data
            mine = team_key in (data.get("destination_team_key"), data.get("source_team_key"))
            if not mine:
                continue
            moves.append(
                Move(
                    when=when,
                    kind=data.get("type", ""),
                    player=(meta.get("name") or {}).get("full", "?"),
                    team=meta.get("editorial_team_abbr", ""),
                    source=data.get("source_type", ""),
                )
            )
    moves.sort(key=lambda m: m.when, reverse=True)
    return moves


# -- projection ---------------------------------------------------------------


def rates(player: Player) -> dict[str, float]:
    """Per-game rates to project with (role-adjusted): L14 when it's a real sample."""
    for window, min_games in (("last14", 3), ("last30", 5), ("season", 3), ("last7", 1)):
        w = player.window(window)
        if w.usable and w.games >= min_games:
            return w.per_game
    return {}


def remaining_days(m: Matchup, today: dt.date) -> list[dt.date]:
    day = max(today, m.start)
    out = []
    while day <= m.end:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


@dataclass
class Projection:
    totals: dict[str, float]  # final projected counting totals + makes/attempts
    added: dict[str, float]  # remaining-week production only
    starts: int = 0
    daily_bench: dict[dt.date, list[Player]] = field(default_factory=dict)


def project(
    current: dict[str, float],
    players: list[Player],
    slots: list[str],
    days: list[dt.date],
    schedule: Schedule,
    available_from: dict[int, dt.date] | None = None,
    unavailable_from: dict[int, dt.date] | None = None,
) -> Projection:
    """Week-end totals if the best lineup is set every remaining day."""
    available_from = available_from or {}
    unavailable_from = unavailable_from or {}
    added: dict[str, float] = {}
    starts = 0
    bench: dict[dt.date, list[Player]] = {}
    for day in days:
        roster = [
            p
            for p in players
            if available_from.get(p.pid, days[0]) <= day
            and (p.pid not in unavailable_from or day < unavailable_from[p.pid])
        ]
        plan = lineup_mod.optimize(roster, slots, date=day, schedule=schedule)
        bench[day] = plan.bench
        for _, player in plan.starters:
            starts += 1
            for stat, value in rates(player).items():
                if stat in ("FG%", "FT%", "3PT%"):
                    continue
                added[stat] = added.get(stat, 0.0) + value
    totals = dict(current)
    for stat, value in added.items():
        totals[stat] = totals.get(stat, 0.0) + value
    return Projection(totals=totals, added=added, starts=starts, daily_bench=bench)


def _pct(totals: dict[str, float], cat: str) -> float:
    made, att = PCT_PARTS[cat]
    return totals.get(made, 0.0) / totals[att] if totals.get(att) else 0.0


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class CatOutlook:
    cat: str
    me_now: float
    opp_now: float
    me_proj: float
    opp_proj: float
    p_win: float

    @property
    def label(self) -> str:
        p = self.p_win
        if p >= 0.85:
            return "Safe W"
        if p >= 0.62:
            return "Lean W"
        if p > 0.38:
            return "Swing"
        if p > 0.15:
            return "Lean L"
        return "Lost"


def outlook(
    cats: list[str],
    me_now: dict[str, float],
    opp_now: dict[str, float],
    me: Projection,
    opp: Projection,
) -> list[CatOutlook]:
    out = []
    for cat in (canonical_cat(c) for c in cats):
        if cat in PCT_PARTS:
            made, att = PCT_PARTS[cat]
            a_now, b_now = _pct(me_now, cat), _pct(opp_now, cat)
            a, b = _pct(me.totals, cat), _pct(opp.totals, cat)
            # Binomial-ish spread on the attempts still to come.
            var = 0.0
            for proj, p in ((me, a), (opp, b)):
                n_rem, n_tot = proj.added.get(att, 0.0), proj.totals.get(att, 0.0)
                if n_tot > 0:
                    var += p * (1 - p) * n_rem / n_tot**2
            sd = math.sqrt(var) * OVERDISPERSION
        else:
            a_now, b_now = me_now.get(cat, 0.0), opp_now.get(cat, 0.0)
            a, b = me.totals.get(cat, 0.0), opp.totals.get(cat, 0.0)
            sd = math.sqrt(me.added.get(cat, 0.0) + opp.added.get(cat, 0.0)) * OVERDISPERSION
        margin = (b - a) if cat in NEGATIVE_CATS else (a - b)
        if sd <= 1e-9:
            p_win = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        else:
            p_win = _phi(margin / sd)
        out.append(CatOutlook(cat, a_now, b_now, a, b, p_win))
    return out


def expected_wins(rows: list[CatOutlook]) -> float:
    """Expected categories won, ignoring the ones you've punted."""
    punted = punt_cats()
    return sum(r.p_win for r in rows if r.cat not in punted)


@dataclass
class Returns:
    """Injured players coming back this week, and who has to go to make room."""

    roster: list[Player]
    available_from: dict[int, dt.date] = field(default_factory=dict)
    unavailable_from: dict[int, dt.date] = field(default_factory=dict)
    forced: list[tuple[Player, Player, dt.date]] = field(default_factory=list)


def apply_returns(
    players: list[Player], returning: dict[str, dt.date], days: list[dt.date]
) -> Returns:
    """Activate returning IL players on their date. Each costs a roster spot,
    filled by dropping the lowest *long-term* value player you're willing to
    drop. Schedule fit is deliberately not the tiebreak: the roster you keep
    matters more than one week's games."""
    from .waivers import drop_pool

    out = Returns(roster=list(players))
    if not days:
        return out
    pool = sorted(drop_pool(players), key=lambda p: p.blended_value)
    for i, player in enumerate(out.roster):
        when = returning.get(player.name.casefold())
        if when is None or when > days[-1] or not player.is_out:
            continue
        back = copy.copy(player)
        back.status = ""
        out.roster[i] = back
        when = max(when, days[0])
        out.available_from[back.pid] = when
        if pool:
            drop = pool.pop(0)
            out.unavailable_from[drop.pid] = when
            out.forced.append((back, drop, when))
    return out


# -- moves --------------------------------------------------------------------


@dataclass
class WeekMove:
    add: Player
    drop: Player
    delta_wins: float
    season_delta: float  # blended value add - drop; negative = costs you long-term
    effective: dt.date
    swings: dict[str, float]  # cat -> change in P(win)
    fit: lineup_mod.Fit | None = None  # starts vs games once he's live
    # Extra players this move costs you beyond the plain drop: when an IL
    # return forces a drop anyway, dropping that same player now just moves
    # the forced drop onto the next one.
    also_drops: list[Player] = field(default_factory=list)


def evaluate_moves(
    cats: list[str],
    matchup: Matchup,
    my_players: list[Player],
    candidates: list[Player],
    opp_proj: Projection,
    slots: list[str],
    days: list[dt.date],
    schedule: Schedule,
    base: list[CatOutlook],
    waiver_days: int = DEFAULT_WAIVER_DAYS,
    returning: dict[str, dt.date] | None = None,
) -> list[WeekMove]:
    """Every add/drop pair that lifts expected categories won, best first."""
    if not days:
        return []
    base_p = {r.cat: r.p_win for r in base}
    base_wins = expected_wins(base)
    moves: list[WeekMove] = []
    from .waivers import drop_pool

    droppable = drop_pool(my_players)
    base_gone = {d.pid for _, d, _ in apply_returns(my_players, returning or {}, days).forced}
    for add in candidates:
        if add.is_out or not rates(add):
            continue
        effective = days[0] + dt.timedelta(days=waiver_days if add.needs_waiver_claim else 0)
        if effective > days[-1]:
            continue
        for drop in droppable:
            roster = [p for p in my_players if p.pid != drop.pid] + [add]
            # The forced IL drop is recomputed on the post-move roster; the
            # dropped player stays only until the move takes effect.
            ret = apply_returns(roster, returning or {}, days)
            proj = project(
                matchup.me.totals,
                ret.roster + [drop],
                slots,
                days,
                schedule,
                available_from={**ret.available_from, add.pid: effective},
                unavailable_from={**ret.unavailable_from, drop.pid: effective},
            )
            rows = outlook(cats, matchup.me.totals, matchup.opp.totals, proj, opp_proj)
            delta = expected_wins(rows) - base_wins
            # Long-term cost = everyone who leaves vs. the no-move plan.
            gone = [drop] + [d for _, d, _ in ret.forced]
            extra = [p for p in gone if p.pid not in base_gone]
            if delta <= 0.05:
                continue
            moves.append(
                WeekMove(
                    add=add,
                    drop=drop,
                    delta_wins=delta,
                    season_delta=add.blended_value - sum(p.blended_value for p in extra),
                    effective=effective,
                    swings={
                        r.cat: r.p_win - base_p[r.cat]
                        for r in rows
                        if abs(r.p_win - base_p[r.cat]) >= 0.03
                    },
                    fit=lineup_mod.lineup_fit(
                        add, ret.roster, slots, schedule, [d for d in days if d >= effective]
                    ),
                    also_drops=[d for _, d, _ in ret.forced],
                )
            )
    moves.sort(key=lambda m: -m.delta_wins)
    return moves


# Long-term first: a move is only offered if the player you add is at least as
# good over the season as the one you drop. Winning a week is the tiebreak.
KEEPER_SAFE_SEASON_DELTA = 0.0


def best_per_add(
    moves: list[WeekMove], top: int = 6, keeper_safe: bool | None = None
) -> list[WeekMove]:
    """One line per add (its best drop). keeper_safe=True keeps only swaps that
    don't cost long-term value; False keeps only the ones that do."""
    seen: set[int] = set()
    out: list[WeekMove] = []
    for move in moves:
        safe = move.season_delta >= KEEPER_SAFE_SEASON_DELTA
        if keeper_safe is not None and safe != keeper_safe:
            continue
        if move.add.pid in seen:
            continue
        seen.add(move.add.pid)
        out.append(move)
        if len(out) >= top:
            break
    return out


# -- orchestration ------------------------------------------------------------


@dataclass
class MatchupReport:
    matchup: Matchup
    today: dt.date
    days: list[dt.date]
    schedule: Schedule
    max_adds: int
    rows: list[CatOutlook]
    rows_if_opp_streams: list[CatOutlook]
    opp_streamers: list[Player]
    me_proj: Projection
    opp_proj: Projection
    opp_players: list[Player]
    my_moves_this_week: list[Move]
    opp_moves_this_week: list[Move]
    best_moves: list[WeekMove]
    forced_drops: list[tuple[Player, Player, dt.date]] = field(default_factory=list)

    @property
    def my_adds_left(self) -> int:
        return max(0, self.max_adds - self.matchup.me.adds_used)

    @property
    def opp_adds_left(self) -> int:
        return max(0, self.max_adds - self.matchup.opp.adds_used)

    def games_left(self, team: str, start: dt.date | None = None) -> int:
        return sum(
            1
            for d in self.days
            if (start is None or d >= start) and self.schedule.plays_on(team, d)
        )


def _streamers(
    candidates: list[Player], days: list[dt.date], schedule: Schedule, k: int
) -> list[Player]:
    """Free agents an opponent would most plausibly stream: healthy, no waiver
    wait, ranked by value x games left this week."""

    def games(p: Player) -> int:
        return sum(1 for d in days if schedule.plays_on(p.team, d))

    pool = [p for p in candidates if not p.is_out and not p.needs_waiver_claim and rates(p)]
    pool.sort(key=lambda p: -(max(p.blended_value, -3.0) + 3.0) * games(p))
    return pool[:k]


def run(client: Any, analysis: Any, today: dt.date | None = None) -> MatchupReport:
    from .players import WINDOW_ORDER, attach_stats, parse_row
    from .valuation import score_players

    today = today or analysis.date
    my_key = client.my_team_key()
    matchup = parse_matchup(client.matchup_raw(), my_key)
    if matchup is None:
        raise SystemExit("No matchup this week (bye or offseason).")
    settings = client.settings()
    max_adds = int(settings.get("max_weekly_adds") or 0)
    waiver_days = int(settings.get("waiver_time") or DEFAULT_WAIVER_DAYS)

    # Opponent roster, valued on the same pools as everyone else.
    opp_players = [parse_row(r, "opp") for r in client.team_roster(matchup.opp.team_key)]
    by_id = {p.pid: p for p in opp_players}
    stats = {w: client.stats(by_id.keys(), w) for w in WINDOW_ORDER}
    attach_stats(by_id, stats, analysis.cats)
    score_players(
        opp_players, analysis.pools, analysis.cats, analysis.weights, analysis.cat_weights
    )

    days = remaining_days(matchup, today)
    schedule = analysis.schedule
    slots = analysis.slots
    returning = returning_players()
    ret = apply_returns(analysis.my_players, returning, days)
    me_proj = project(
        matchup.me.totals, ret.roster, slots, days, schedule,
        available_from=ret.available_from, unavailable_from=ret.unavailable_from,
    )
    opp_proj = project(matchup.opp.totals, opp_players, slots, days, schedule)
    rows = outlook(analysis.cats, matchup.me.totals, matchup.opp.totals, me_proj, opp_proj)

    # Stress test: the opponent spends adds on the best free-agent streamers,
    # each replacing their weakest player from today on.
    opp_left = max(0, max_adds - matchup.opp.adds_used)
    streamers = _streamers(analysis.available, days, schedule, min(opp_left, 3))
    # Injured players go first: they cost nothing this week, so that's the
    # worst case for you (pure added games).
    worst = sorted(opp_players, key=lambda p: (not p.is_out, p.blended_value))[
        : len(streamers)
    ]
    streamed = project(
        matchup.opp.totals,
        opp_players + streamers,
        slots,
        days,
        schedule,
        unavailable_from={p.pid: days[0] for p in worst} if days else {},
    )
    rows_streamed = outlook(
        analysis.cats, matchup.me.totals, matchup.opp.totals, me_proj, streamed
    )

    best: list[WeekMove] = []
    if max_adds - matchup.me.adds_used > 0:
        best = evaluate_moves(
            analysis.cats, matchup, analysis.my_players, analysis.available,
            opp_proj, slots, days, schedule, rows, waiver_days=waiver_days,
            returning=returning,
        )

    my_moves = parse_transactions(client.transactions_raw(my_key), my_key, matchup.start)
    opp_key = matchup.opp.team_key
    opp_moves = parse_transactions(client.transactions_raw(opp_key), opp_key, matchup.start)
    return MatchupReport(
        matchup=matchup, today=today, days=days, schedule=schedule, max_adds=max_adds,
        rows=rows, rows_if_opp_streams=rows_streamed, opp_streamers=streamers,
        me_proj=me_proj, opp_proj=opp_proj, opp_players=opp_players,
        my_moves_this_week=my_moves, opp_moves_this_week=opp_moves, best_moves=best,
        forced_drops=ret.forced,
    )
