"""Player model: merge Yahoo's roster/pool rows with multi-window stats."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import canonical_cat
from .statmap import RATIO_FIELDS

WINDOW_ORDER = ["last7", "last14", "last30", "season"]
WINDOW_LABELS = {"last7": "L7", "last14": "L14", "last30": "L30", "season": "Season"}

# Role adjustment: each window's per-game production is rescaled to the
# player's *current* minutes. Per-minute production is stable; minutes are
# what change with a role. Clamped so a garbage-time blip can't triple a line.
ROLE_SCALE_MIN, ROLE_SCALE_MAX = 0.6, 1.5
# Games needed before a window's minutes define "current role".
ROLE_MIN_GAMES = {"last14": 3.0, "last7": 2.0}
# Never rescale minutes or the reported rate stats themselves.
UNSCALED = {"MIN", "FG%", "FT%", "3PT%", "GP", "GS"}

# Statuses that mean "not going to play for a while".
OUT_STATUSES = {"O", "INJ", "IR", "IL", "NA", "SUSP"}
QUESTIONABLE_STATUSES = {"GTD", "DTD", "Q", "P"}


@dataclass
class Window:
    """One stat window for one player, as per-game rates."""

    games: float = 0.0
    per_game: dict[str, float] = field(default_factory=dict)
    totals: dict[str, float] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.games > 0 and bool(self.per_game)

    @property
    def mpg(self) -> float:
        """Raw minutes per game (never role-adjusted)."""
        return self.totals.get("MIN", 0.0) / self.games if self.games > 0 else 0.0


@dataclass
class Player:
    pid: int
    name: str
    positions: list[str] = field(default_factory=list)
    status: str = ""
    team: str = ""
    pct_owned: float = 0.0
    source: str = "pool"  # roster | waivers | fa | taken
    selected_position: str | None = None
    windows: dict[str, Window] = field(default_factory=dict)
    # Never suggested as a drop: high-ownership (the crowd's read on upside a
    # box score can't see, e.g. a #1-pick rookie) or named in PROTECTED_PLAYERS.
    protected: bool = False
    # A roster spot you churn: the only players suggested as drops when any
    # are set (STREAMERS in .env, plus anything you hold that's < 50% owned).
    streamer: bool = False

    # Filled in by valuation.
    z: dict[str, dict[str, float]] = field(default_factory=dict)
    value: dict[str, float] = field(default_factory=dict)
    blended_z: dict[str, float] = field(default_factory=dict)
    blended_value: float = 0.0
    trend: float = 0.0

    @property
    def is_out(self) -> bool:
        return self.status.upper() in OUT_STATUSES

    @property
    def is_questionable(self) -> bool:
        return self.status.upper() in QUESTIONABLE_STATUSES

    @property
    def on_my_roster(self) -> bool:
        return self.source == "roster"

    @property
    def needs_waiver_claim(self) -> bool:
        return self.source == "waivers"

    def window(self, name: str) -> Window:
        return self.windows.get(name, Window())

    def games_per_week(self, name: str = "last30") -> float:
        """Rough pace of games -- useful context, not a projection."""
        window = self.window(name)
        if not window.usable:
            return 0.0
        span_days = {"last7": 7, "last14": 14, "last30": 30, "season": 30}.get(name, 30)
        return window.games * 7.0 / span_days

    def label(self) -> str:
        bits = [self.name]
        if self.team:
            bits.append(self.team)
        if self.positions:
            bits.append("/".join(self.positions))
        return " ".join(bits)


def parse_row(row: dict, source: str) -> Player:
    """Turn one Yahoo roster/pool row into a Player."""
    return Player(
        pid=int(row["player_id"]),
        name=row.get("name", f"player {row.get('player_id')}"),
        positions=[p for p in row.get("eligible_positions", []) if p not in {"IL", "IL+", "BN"}],
        status=(row.get("status") or "").strip(),
        team=(row.get("editorial_team_abbr") or "").strip().upper(),
        pct_owned=float(row.get("percent_owned") or 0),
        source=source,
        selected_position=row.get("selected_position"),
    )


def _split_ratio(value: Any) -> tuple[float | None, float | None]:
    """Parse Yahoo's 'made/attempted' composite values, e.g. '123/250'."""
    if not isinstance(value, str) or "/" not in value:
        return None, None
    made, _, attempted = value.partition("/")
    try:
        return float(made.strip()), float(attempted.strip())
    except ValueError:
        return None, None


def parse_window(raw: dict, cats: Iterable[str]) -> Window:
    """Normalize one stat payload into per-game rates.

    Yahoo returns window totals (plus GP), so everything is divided by games
    played. Made/attempted splits are recovered from the discrete stat ids when
    present and from the "FGM/A" composite string otherwise, because valuing
    FG% needs volume: 2-for-2 is not the same contribution as 10-for-20.
    """
    if not raw:
        return Window()

    totals: dict[str, float] = {}
    for key, value in raw.items():
        if key in RATIO_FIELDS:
            made, attempted = _split_ratio(value)
            made_key, attempted_key = RATIO_FIELDS[key]
            if made is not None:
                totals.setdefault(made_key, made)
            if attempted is not None:
                totals.setdefault(attempted_key, attempted)
            continue
        if isinstance(value, (int, float)):
            totals[canonical_cat(key)] = float(value)

    games = float(totals.get("GP") or 0)
    if games <= 0:
        return Window(games=0.0, totals=totals)

    wanted = {canonical_cat(c) for c in cats} | {"FGM", "FGA", "FTM", "FTA", "MIN"}
    per_game = {
        name: value / games
        for name, value in totals.items()
        if name in wanted and name not in {"FG%", "FT%", "3PT%"}
    }
    # Keep the league-reported rates for display.
    for rate in ("FG%", "FT%", "3PT%"):
        if rate in totals:
            per_game[rate] = totals[rate]

    return Window(games=games, per_game=per_game, totals=totals)


def attach_stats(
    players: dict[int, Player],
    stats_by_window: dict[str, dict[int, dict]],
    cats: Iterable[str],
) -> None:
    """Fill each player's `windows` from the fetched per-window payloads."""
    cats = list(cats)
    for window_name, rows in stats_by_window.items():
        for pid, raw in rows.items():
            player = players.get(int(pid))
            if player is not None:
                player.windows[window_name] = parse_window(raw, cats)
    for player in players.values():
        role_adjust(player)


def current_minutes(player: Player) -> float | None:
    """Minutes per game in the player's current role: L14, else L7."""
    for window, min_games in ROLE_MIN_GAMES.items():
        w = player.window(window)
        if w.usable and w.games >= min_games and w.mpg > 0:
            return w.mpg
    return None


def role_adjust(player: Player) -> None:
    """Rescale every window's per-game production to current minutes.

    A bench guy who just moved into the starting five has a season line built
    on 18 minutes; at 32 minutes his per-minute production says far more.
    Shooting *efficiency* is untouched (makes and attempts scale together), so
    percentage categories only gain volume.
    """
    now = current_minutes(player)
    if not now:
        return
    for window in player.windows.values():
        if not window.usable or window.mpg <= 0:
            continue
        scale = max(ROLE_SCALE_MIN, min(ROLE_SCALE_MAX, now / window.mpg))
        if abs(scale - 1.0) < 1e-9:
            continue
        window.per_game = {
            stat: value if stat in UNSCALED else value * scale
            for stat, value in window.per_game.items()
        }


def dedupe(*groups: Iterable[Player]) -> dict[int, Player]:
    """Merge player groups, with earlier groups winning on conflicts.

    Order matters: pass your roster first so a player who also shows up in a
    pool listing stays tagged as yours.
    """
    merged: dict[int, Player] = {}
    for group in groups:
        for player in group:
            existing = merged.get(player.pid)
            if existing is None:
                merged[player.pid] = player
            elif not existing.pct_owned and player.pct_owned:
                existing.pct_owned = player.pct_owned
    return merged
