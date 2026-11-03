"""NBA schedule lookup, so lineup advice knows who actually plays on a date.

Yahoo's fantasy API has no "does this player's team play on date X", so the
schedule comes from ESPN's public scoreboard feed (no key required). One call
per date, cached on disk, for however many days ahead we care about.

If the feed can't be reached, `Schedule.available` is False and `plays_on`
answers True for everyone -- callers then say "schedule unavailable" in the
report instead of silently benching people.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field

import requests

from .config import CACHE_DIR, read_json, save_json

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
CACHE_FILE = CACHE_DIR / "nba-schedule.json"
FUTURE_TTL = 43200  # 12h -- future dates can still be rescheduled
PAST_TTL = 86400 * 30  # a played date never changes

# Every abbreviation variant we might see (ESPN, Yahoo, other feeds) mapped to
# a single canonical tricode. ESPN uses GS/NO/NY/SA/UTAH/WSH; Yahoo uses
# GSW/NOP/NYK/SAS/UTA/WAS and PHO for Phoenix.
TEAM_ALIASES = {
    "GS": "GSW",
    "NO": "NOP",
    "NOR": "NOP",
    "NY": "NYK",
    "SA": "SAS",
    "UTAH": "UTA",
    "WSH": "WAS",
    "PHO": "PHX",
    "BRK": "BKN",
    "BRO": "BKN",
    "CHO": "CHA",
}


def normalize_team(abbr: str) -> str:
    abbr = (abbr or "").strip().upper()
    return TEAM_ALIASES.get(abbr, abbr)


@dataclass
class Schedule:
    """Which teams play on which dates."""

    by_date: dict[str, set[str]] = field(default_factory=dict)
    available: bool = True
    error: str = ""

    def teams_on(self, date: dt.date) -> set[str]:
        return self.by_date.get(date.isoformat(), set())

    def plays_on(self, team_abbr: str, date: dt.date) -> bool:
        """True if the team plays that date -- or if we have no data at all."""
        if not self.available:
            return True
        return normalize_team(team_abbr) in self.teams_on(date)

    def game_count(self, team_abbr: str, start: dt.date, days: int) -> int:
        """Games for a team over `days` starting at `start` (inclusive)."""
        if not self.available:
            return 0
        team = normalize_team(team_abbr)
        return sum(
            1
            for offset in range(days)
            if team in self.teams_on(start + dt.timedelta(days=offset))
        )

    def known_dates(self) -> int:
        return len(self.by_date)


def _load_cache() -> dict:
    cached = read_json(CACHE_FILE, {})
    return cached if isinstance(cached, dict) else {}


def _fetch_date(date: dt.date) -> list[str] | None:
    """Team tricodes playing on one date, or None if the request failed."""
    try:
        response = requests.get(
            SCOREBOARD,
            params={"dates": date.strftime("%Y%m%d")},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    teams: set[str] = set()
    for event in payload.get("events", []):
        for competition in event.get("competitions", []):
            for competitor in competition.get("competitors", []):
                abbr = (competitor.get("team") or {}).get("abbreviation")
                if abbr:
                    teams.add(normalize_team(abbr))
    return sorted(teams)


def load(
    start: dt.date | None = None, days: int = 14, use_cache: bool = True
) -> Schedule:
    """Build a schedule covering `days` dates starting at `start` (today).

    An empty day is a real answer, not a failure -- the NBA schedules no games
    on US election day, for instance.
    """
    start = start or dt.date.today()
    today = dt.date.today()
    cache = _load_cache() if use_cache else {}
    by_date: dict[str, set[str]] = {}
    failures = 0
    dirty = False

    for offset in range(days):
        date = start + dt.timedelta(days=offset)
        key = date.isoformat()
        entry = cache.get(key)
        ttl = PAST_TTL if date < today else FUTURE_TTL
        if (
            use_cache
            and isinstance(entry, dict)
            and time.time() - entry.get("_at", 0) < ttl
        ):
            by_date[key] = set(entry.get("teams", []))
            continue

        teams = _fetch_date(date)
        if teams is None:
            failures += 1
            continue
        by_date[key] = set(teams)
        cache[key] = {"_at": time.time(), "teams": teams}
        dirty = True

    if dirty:
        save_json(CACHE_FILE, cache)

    if failures and not by_date:
        return Schedule(available=False, error="ESPN schedule feed unreachable")
    schedule = Schedule(by_date=by_date)
    if failures:
        schedule.error = f"{failures} of {days} dates could not be fetched"
    return schedule
