"""Project paths, .env loading, and the Yahoo credential file."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
SECRETS_DIR = ROOT / ".secrets"
OAUTH_FILE = SECRETS_DIR / "oauth2.json"
CACHE_DIR = ROOT / ".cache"
REPORTS_DIR = ROOT / "reports"

# Yahoo's 9-category basketball set. TURNOVERS is the one you want less of.
NINE_CAT = ["FG%", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]
NEGATIVE_CATS = {"TO", "TOV", "PF"}

# Yahoo display names vary slightly by league; normalize to the names above.
CAT_ALIASES = {
    "FGP": "FG%",
    "FG_PCT": "FG%",
    "FTP": "FT%",
    "FT_PCT": "FT%",
    "3PT": "3PTM",
    "3": "3PTM",
    "3PTA": "3PTA",
    "TREB": "REB",
    "STL": "ST",
    "TOV": "TO",
}


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Read a .env file into os.environ without clobbering real env vars.

    Deliberately tiny so the project needs no python-dotenv dependency.
    """
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if value:
                values[key] = value
                os.environ.setdefault(key, value)
    return values


def env(key: str, default: str | None = None) -> str | None:
    load_env()
    return os.environ.get(key) or default


DEFAULT_PROTECT_OWNED_PCT = 85.0


def protect_owned_pct() -> float:
    """Players rostered in at least this % of Yahoo leagues are never drops."""
    try:
        return float(env("PROTECT_OWNED_PCT") or DEFAULT_PROTECT_OWNED_PCT)
    except ValueError:
        return DEFAULT_PROTECT_OWNED_PCT


def protected_names() -> set[str]:
    """PROTECTED_PLAYERS in .env: comma-separated names you'll never drop."""
    raw = env("PROTECTED_PLAYERS") or ""
    return {name.strip().casefold() for name in raw.split(",") if name.strip()}


STREAMER_OWNED_PCT = 50.0


def streamer_names() -> set[str]:
    """STREAMERS in .env: comma-separated roster spots you treat as churn."""
    raw = env("STREAMERS") or ""
    return {name.strip().casefold() for name in raw.split(",") if name.strip()}


def punt_cats() -> set[str]:
    """PUNT_CATS in .env: categories you've conceded (e.g. TO). They get zero
    weight in player values and don't count toward expected wins."""
    raw = env("PUNT_CATS") or ""
    return {canonical_cat(c) for c in raw.split(",") if c.strip()}


def returning_players() -> dict[str, "dt.date"]:
    """RETURNING in .env: 'Name:YYYY-MM-DD,...' for injured players expected
    back. Activating one forces a drop, which the matchup plan accounts for."""
    import datetime as dt

    out: dict[str, dt.date] = {}
    for item in (env("RETURNING") or "").split(","):
        name, _, day = item.rpartition(":")
        try:
            out[name.strip().casefold()] = dt.date.fromisoformat(day.strip())
        except ValueError:
            continue
    return out


def require_env(key: str, hint: str = "") -> str:
    value = env(key)
    if not value:
        raise SystemExit(
            f"Missing {key} in .env. Copy .env.example to .env and fill it in."
            + (f"\n{hint}" if hint else "")
        )
    return value


def set_env_values(updates: dict[str, str], path: Path = ENV_FILE) -> None:
    """Upsert keys in .env, preserving comments and ordering."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    out.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def ensure_oauth_file() -> Path:
    """yahoo_oauth wants credentials in a JSON file; build it from .env.

    It also writes the access/refresh tokens back into this same file, which is
    why it lives in gitignored .secrets/.
    """
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    client_id = require_env(
        "YAHOO_CLIENT_ID",
        hint="Create an app at https://developer.yahoo.com/apps/create/ "
        "(Fantasy Sports -> Read, redirect URI 'oob').",
    )
    client_secret = require_env("YAHOO_CLIENT_SECRET")

    existing = read_json(OAUTH_FILE, {}) or {}
    if not isinstance(existing, dict):
        existing = {}
    # Rotating credentials invalidates cached tokens, so start clean.
    if existing.get("consumer_key") != client_id:
        existing = {}
    existing["consumer_key"] = client_id
    existing["consumer_secret"] = client_secret
    # Yahoo demands the same redirect_uri on refresh as on the initial grant,
    # and yahoo_oauth reads this key straight out of the file.
    existing["callback_uri"] = env("YAHOO_REDIRECT_URI", "https://localhost:8080")
    save_json(OAUTH_FILE, existing)
    return OAUTH_FILE


@dataclass(frozen=True)
class WindowWeights:
    """How much each stat window counts toward a player's headline value.

    Recency-tilted on purpose: for a waiver decision, what someone has done
    over the last week or two matters far more than an October baseline.
    """

    # The last two weeks carry 65%: L14 is the anchor (big enough to trust,
    # recent enough to reflect the current role), L7 catches what's breaking.
    last7: float = 0.25
    last14: float = 0.40
    last30: float = 0.20
    season: float = 0.15

    def normalized(self) -> dict[str, float]:
        raw = {
            "last7": self.last7,
            "last14": self.last14,
            "last30": self.last30,
            "season": self.season,
        }
        total = sum(raw.values())
        if total <= 0:
            raise ValueError("window weights must sum to > 0")
        return {window: weight / total for window, weight in raw.items()}

    @classmethod
    def parse(cls, spec: str | None) -> "WindowWeights":
        """Parse '0.25,0.4,0.2,0.15' (last7,last14,last30,season) from the CLI."""
        if not spec:
            return cls()
        parts = [float(p) for p in spec.replace(" ", "").split(",")]
        if len(parts) != 4:
            raise SystemExit("--weights needs four numbers: last7,last14,last30,season")
        return cls(*parts)


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def read_json(path: Path, default: object = None) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def canonical_cat(name: str) -> str:
    name = str(name).strip()
    return CAT_ALIASES.get(name.upper(), name)
