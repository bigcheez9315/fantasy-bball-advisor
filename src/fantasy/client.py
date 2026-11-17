"""Authenticated Yahoo access, with disk caching so reruns are cheap.

Transport is the `yahoo_fantasy_api` SDK (which handles Yahoo's paging and its
awkward nested JSON). Everything on top of it -- valuation, waiver logic,
lineups -- lives in this package.
"""
from __future__ import annotations

import datetime as dt

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import yahoo_fantasy_api as yfa
from yahoo_oauth import OAuth2

from .config import (
    CACHE_DIR,
    canonical_cat,
    ensure_oauth_file,
    env,
    read_json,
    save_json,
)
from . import auth
from .statmap import NBA_STAT_IDS, install_stat_map

API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
NON_STARTING = {"BN", "IL", "IL+", "IR", "NA"}


def slots_from_positions(positions: dict) -> list[str]:
    """Expand the SDK's `positions()` map into a flat list of startable slots."""
    slots: list[str] = []
    for position, info in (positions or {}).items():
        if position in NON_STARTING:
            continue
        info = info or {}
        if str(info.get("is_starting_position", 1)) == "0":
            continue
        slots.extend([position] * int(info.get("count", 0) or 0))
    return slots


def _to_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_players_stats(payload: dict) -> list[dict]:
    """Flatten a game-level `/players;player_keys=.../stats` response.

    Yahoo nests each player as `{"player": [[meta dicts...], {"player_stats":
    {"stats": [{"stat": {"stat_id", "value"}}, ...]}}]}`. Output rows match
    what `players.parse_window` expects: named numeric totals (GP included),
    with made/attempted kept as discrete FGM/FGA/FTM/FTA.
    """
    players = (payload.get("fantasy_content") or {}).get("players") or {}
    rows: list[dict] = []
    for key, entry in players.items():
        if key == "count" or not isinstance(entry, dict):
            continue
        parts = entry.get("player") or []
        row: dict[str, Any] = {}
        for part in parts:
            if isinstance(part, list):  # metadata: list of single-key dicts
                for item in part:
                    if not isinstance(item, dict):
                        continue
                    if "player_id" in item:
                        row["player_id"] = int(item["player_id"])
                    if "name" in item and isinstance(item["name"], dict):
                        row["name"] = item["name"].get("full", "")
            elif isinstance(part, dict) and "player_stats" in part:
                for stat in part["player_stats"].get("stats", []):
                    stat = stat.get("stat", {})
                    name = NBA_STAT_IDS.get(int(stat.get("stat_id", -1)))
                    number = _to_number(stat.get("value"))
                    if name and number is not None:
                        row[name] = number
        if "player_id" in row:
            rows.append(row)
    return rows

# yahoo_oauth logs every token check at INFO; quiet it down.
logging.getLogger("yahoo_oauth").setLevel(logging.WARNING)

# Our window names -> the SDK's req_type values.
# "biweekly" is undocumented but verified (2026-11-17) to equal the sum of the
# last 14 days' daily stat lines.
WINDOWS = {
    "last7": "lastweek",
    "last14": "biweekly",
    "last30": "lastmonth",
    "season": "season",
}

DEFAULT_TTL = {
    "settings": 86400,   # league rules barely change
    "roster": 900,       # 15 min
    "players": 1800,     # 30 min
    "stats": 3600,       # 1 hr; Yahoo updates these a few times a day
    "standings": 1800,
}


@dataclass
class FantasyClient:
    league_key: str | None = None
    use_cache: bool = True
    ttl_overrides: dict[str, int] = field(default_factory=dict)
    api_calls: int = 0
    _memo: dict[str, Any] = field(default_factory=dict, repr=False)
    _oauth: OAuth2 | None = field(default=None, repr=False)
    _league: Any = field(default=None, repr=False)

    # -- plumbing -----------------------------------------------------------

    def _ttl(self, kind: str) -> int:
        return self.ttl_overrides.get(kind, DEFAULT_TTL.get(kind, 1800))

    def _cached(self, kind: str, key: str, fetch: Callable[[], Any]) -> Any:
        """Memoize a fetch for the lifetime of this process only.

        Deliberately in-memory: the Yahoo API Access and Use Agreement says
        "Developer shall not store, cache or index the Yahoo Fantasy
        Information", so nothing from Yahoo is written to disk. This keeps a
        single run from re-requesting the same resource (analysis.run touches
        settings and rosters repeatedly) without persisting anything.

        The practical cost is that every run refetches. Keep `candidate_limit`
        sane rather than reaching for a disk cache.
        """
        memo_key = f"{kind}:{key}"
        if self.use_cache and memo_key in self._memo:
            return self._memo[memo_key]
        body = fetch()
        self.api_calls += 1
        self._memo[memo_key] = body
        return body

    @property
    def oauth(self) -> OAuth2:
        if self._oauth is None:
            path = ensure_oauth_file()
            if not auth.have_token():
                # Don't let yahoo_oauth's own prompt fire here: it hardcodes the
                # legacy 'oob' redirect, which modern Yahoo apps reject.
                raise SystemExit(
                    "Not logged in to Yahoo yet. Run this in a terminal:\n"
                    "    python -m fantasy auth"
                )
            self._oauth = OAuth2(None, None, from_file=str(path))
            if not self._oauth.token_is_valid():
                credentials = self._oauth.refresh_access_token()
                stored = read_json(path, {}) or {}
                stored.update(credentials)
                save_json(path, stored)
        return self._oauth

    def game(self) -> yfa.Game:
        return yfa.Game(self.oauth, "nba")

    def league(self) -> Any:
        if self._league is None:
            key = self.league_key or env("LEAGUE_KEY")
            if not key:
                raise SystemExit(
                    "No league key. Run `python -m fantasy leagues` to list "
                    "yours, then `python -m fantasy --league <key> init`."
                )
            self.league_key = key
            self._league = self.game().to_league(key)
            # Teach the SDK the NBA stat ids it lacks (GP, FGM/FGA, FTM/FTA).
            install_stat_map(self._league)
        return self._league

    # -- discovery ----------------------------------------------------------

    def my_leagues(self) -> list[dict]:
        """Every NBA league this account is in, newest season first."""
        game = self.game()
        out = []
        for league_id in game.league_ids():
            try:
                settings = game.to_league(league_id).settings()
            except Exception as exc:  # a long-dead league can 400; skip it
                out.append({"league_key": league_id, "error": str(exc)[:120]})
                continue
            out.append(
                {
                    "league_key": league_id,
                    "name": settings.get("name"),
                    "season": settings.get("season"),
                    "num_teams": settings.get("num_teams"),
                    "scoring": settings.get("scoring_type"),
                }
            )
        return out

    def settings(self) -> dict:
        key = self.league_key or ""
        return self._cached("settings", f"settings:{key}", lambda: self.league().settings())

    def categories(self) -> list[str]:
        """Scoring categories, normalized to canonical 9-cat names."""
        key = self.league_key or ""
        raw = self._cached(
            "settings", f"cats:{key}", lambda: self.league().stat_categories()
        )
        return [canonical_cat(c["display_name"]) for c in raw]

    def roster_slots(self) -> list[str]:
        """Startable lineup slots, e.g. ['PG','SG','G','SF','PF','F','C','Util',...].

        `settings()` does not carry roster positions on real leagues; the SDK
        exposes them separately via `positions()`.
        """
        key = self.league_key or ""
        positions = self._cached(
            "settings", f"positions:{key}", lambda: self.league().positions()
        )
        return slots_from_positions(positions)

    def my_team_key(self) -> str:
        explicit = env("TEAM_KEY")
        if explicit:
            return explicit
        key = self.league_key or ""
        return self._cached("settings", f"teamkey:{key}", lambda: self.league().team_key())

    def team(self, team_key: str | None = None) -> Any:
        return self.league().to_team(team_key or self.my_team_key())

    def _get_json(self, path: str) -> dict:
        sep = "&" if "?" in path else "?"
        response = self.oauth.session.get(f"{API_BASE}/{path}{sep}format=json")
        response.raise_for_status()
        self.api_calls += 1
        return response.json()

    def matchup_raw(self, week: int | None = None) -> dict:
        """This week's H2H matchup: both teams' week-to-date totals."""
        week = week or self.league().current_week()
        team_key = self.my_team_key()
        return self._cached(
            "roster", f"matchup:{team_key}:{week}",
            lambda: self._get_json(f"team/{team_key}/matchups;weeks={week}"),
        )

    def transactions_raw(self, team_key: str) -> dict:
        league_key = self.league_key or self.league().league_id
        return self._cached(
            "roster", f"tx:{team_key}",
            lambda: self._get_json(
                f"league/{league_key}/transactions;team_key={team_key};types=add,drop"
            ),
        )

    def team_roster(self, team_key: str) -> list[dict]:
        return self._cached(
            "roster", f"roster:{team_key}:today", lambda: self.team(team_key).roster()
        )

    # -- players ------------------------------------------------------------

    def my_roster(self, day: str | None = None) -> list[dict]:
        team_key = self.my_team_key()
        label = f"roster:{team_key}:{day or 'today'}"
        fetch = (lambda: self.team(team_key).roster(day=day)) if day else (
            lambda: self.team(team_key).roster()
        )
        return self._cached("roster", label, fetch)

    def available_players(self, status: str = "A") -> list[dict]:
        """Everyone who can be picked up. status: A=all, FA=free agents, W=waivers.

        This pages through the entire pool (Yahoo gives 25 per call), so it is
        the most expensive call here -- hence the cache.
        """
        key = self.league_key or ""
        def fetch() -> list[dict]:
            league = self.league()
            if status == "W":
                return league.waivers()
            if status == "FA":
                return league.free_agents("P")
            # 'A' isn't exposed directly by the SDK's helpers; go via internals
            # so we get free agents and waiver players in one pass.
            return league._fetch_players("A")

        return self._cached("players", f"avail:{key}:{status}", fetch)

    def taken_players(self) -> list[dict]:
        """Every player on a roster league-wide -- the valuation baseline."""
        key = self.league_key or ""
        return self._cached(
            "players", f"taken:{key}", lambda: self.league().taken_players()
        )

    def stats(self, player_ids: Iterable[int], window: str) -> dict[int, dict]:
        """Per-window raw stats keyed by player id.

        Cached per (window, id) so adding a few players to a later run only
        fetches the new ones.
        """
        req_type = WINDOWS.get(window)
        if req_type is None:
            raise ValueError(f"unknown window {window!r}; expected {list(WINDOWS)}")

        wanted = [int(p) for p in dict.fromkeys(player_ids)]
        out: dict[int, dict] = {}
        missing: list[int] = []

        # In-memory only, per the agreement's no-caching clause. Still worth
        # doing: a run asks for overlapping player sets across windows.
        for pid in wanted:
            memo_key = f"stats:{window}:{pid}"
            if self.use_cache and memo_key in self._memo:
                out[pid] = self._memo[memo_key]
            else:
                missing.append(pid)

        if missing:
            # Not league.player_stats(): in league context Yahoo returns only
            # the scoring categories -- no GP, so window totals can't become
            # per-game rates. The game-level endpoint returns the full set.
            fetched: list[dict] = []
            game_key = self.league().league_id.split(".l.")[0]
            for start in range(0, len(missing), 25):
                chunk = missing[start : start + 25]
                keys = ",".join(f"{game_key}.p.{pid}" for pid in chunk)
                url = (
                    f"{API_BASE}/players;player_keys={keys}"
                    f"/stats;type={req_type}?format=json"
                )
                response = self.oauth.session.get(url)
                response.raise_for_status()
                fetched.extend(parse_players_stats(response.json()))
                self.api_calls += 1
            by_id = {int(row["player_id"]): row for row in fetched if "player_id" in row}
            for pid in missing:
                row = by_id.get(pid, {})
                out[pid] = row
                self._memo[f"stats:{window}:{pid}"] = row

        return out

    def day_stats(self, player_ids: Iterable[int], day: dt.date) -> dict[int, dict]:
        """One day's stat line per player (GP 0 = no game). Not cached."""
        ids = [int(p) for p in dict.fromkeys(player_ids)]
        game_key = self.league().league_id.split(".l.")[0]
        rows: list[dict] = []
        for start in range(0, len(ids), 25):
            keys = ",".join(f"{game_key}.p.{pid}" for pid in ids[start : start + 25])
            url = (
                f"{API_BASE}/players;player_keys={keys}"
                f"/stats;type=date;date={day.isoformat()}?format=json"
            )
            response = self.oauth.session.get(url)
            response.raise_for_status()
            rows.extend(parse_players_stats(response.json()))
            self.api_calls += 1
        return {int(r["player_id"]): r for r in rows}

    def find_player(self, name: str) -> dict | None:
        """Search any player by name; returns Player(**kwargs) fields."""
        hits = self.league().player_details(name)
        self.api_calls += 1
        if not hits:
            return None
        exact = [h for h in hits if h["name"]["full"].casefold() == name.casefold()]
        hit = (exact or hits)[0]
        return {
            "pid": int(hit["player_id"]),
            "name": hit["name"]["full"],
            "team": hit.get("editorial_team_abbr", "").upper(),
            "positions": [
                e["position"] for e in hit.get("eligible_positions", [])
                if e.get("position") not in {"IL", "IL+", "BN", "Util"}
            ],
            "status": hit.get("status", "") or "",
            "source": "other",
        }

    def standings(self) -> list[dict]:
        key = self.league_key or ""
        return self._cached("standings", f"standings:{key}", lambda: self.league().standings())

    def percent_owned(self, player_ids: Iterable[int]) -> dict[int, float]:
        ids = [int(p) for p in dict.fromkeys(player_ids)]
        if not ids:
            return {}
        key = self.league_key or ""
        digest = hashlib.sha256(",".join(map(str, sorted(ids))).encode()).hexdigest()[:12]
        rows = self._cached(
            "players",
            f"pctown:{key}:{digest}",
            lambda: self.league().percent_owned(ids),
        )
        return {
            int(r["player_id"]): float(r.get("percent_owned") or 0)
            for r in rows
            if "player_id" in r
        }
