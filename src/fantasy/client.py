"""Authenticated Yahoo access, with disk caching so reruns are cheap.

Transport is the `yahoo_fantasy_api` SDK (which handles Yahoo's paging and its
awkward nested JSON). Everything on top of it -- valuation, waiver logic,
lineups -- lives in this package.
"""
from __future__ import annotations

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
from .statmap import install_stat_map

# yahoo_oauth logs every token check at INFO; quiet it down.
logging.getLogger("yahoo_oauth").setLevel(logging.WARNING)

# Our window names -> the SDK's req_type values.
WINDOWS = {"last7": "lastweek", "last30": "lastmonth", "season": "season"}

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
    _oauth: OAuth2 | None = field(default=None, repr=False)
    _league: Any = field(default=None, repr=False)

    # -- plumbing -----------------------------------------------------------

    def _ttl(self, kind: str) -> int:
        return self.ttl_overrides.get(kind, DEFAULT_TTL.get(kind, 1800))

    def _cached(self, kind: str, key: str, fetch: Callable[[], Any]) -> Any:
        """Memoize a fetch on disk, keyed by kind + a stable key string."""
        digest = hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:20]
        path = CACHE_DIR / f"{kind}-{digest}.json"
        if self.use_cache:
            cached = read_json(path)
            if isinstance(cached, dict) and time.time() - cached.get("_at", 0) < self._ttl(kind):
                return cached["body"]
        body = fetch()
        self.api_calls += 1
        save_json(path, {"_at": time.time(), "key": key, "body": body})
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
                    "yours, then `python -m fantasy init --league <key>`."
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
        """Startable lineup slots, e.g. ['PG','SG','G','SF','PF','F','C','Util',...]."""
        settings = self.settings()
        slots: list[str] = []
        for entry in settings.get("roster_positions", []):
            position = entry.get("position")
            count = int(entry.get("count", 0) or 0)
            if position in {"BN", "IL", "IL+", "IR", "NA"}:
                continue
            slots.extend([position] * count)
        return slots

    def my_team_key(self) -> str:
        explicit = env("TEAM_KEY")
        if explicit:
            return explicit
        key = self.league_key or ""
        return self._cached("settings", f"teamkey:{key}", lambda: self.league().team_key())

    def team(self, team_key: str | None = None) -> Any:
        return self.league().to_team(team_key or self.my_team_key())

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
        league_key = self.league_key or ""
        out: dict[int, dict] = {}
        missing: list[int] = []

        for pid in wanted:
            cached = None
            if self.use_cache:
                digest = hashlib.sha256(
                    f"stats:{league_key}:{window}:{pid}".encode()
                ).hexdigest()[:20]
                path = CACHE_DIR / f"stats-{digest}.json"
                cached = read_json(path)
            if (
                isinstance(cached, dict)
                and time.time() - cached.get("_at", 0) < self._ttl("stats")
            ):
                out[pid] = cached["body"]
            else:
                missing.append(pid)

        if missing:
            league = self.league()
            fetched = league.player_stats(missing, req_type)
            self.api_calls += max(1, len(missing) // 25)
            by_id = {int(row["player_id"]): row for row in fetched if "player_id" in row}
            for pid in missing:
                row = by_id.get(pid, {})
                out[pid] = row
                digest = hashlib.sha256(
                    f"stats:{league_key}:{window}:{pid}".encode()
                ).hexdigest()[:20]
                save_json(CACHE_DIR / f"stats-{digest}.json", {"_at": time.time(), "body": row})

        return out

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
