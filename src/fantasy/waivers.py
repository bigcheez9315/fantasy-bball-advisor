"""Add/drop recommendations: who on the wire beats who on your roster.

The headline number for a swap is the blended 9-cat z-score difference between
the player you'd add and the player you'd drop. Everything else here exists to
keep that number honest -- schedule load, injury status, sample size, roster
legality, and whether a hot week is actually a role change or just variance.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import NEGATIVE_CATS
from .lineup import fillable_slots
from .players import Player
from .schedule import Schedule
from .valuation import category_deltas

# Below this, a swap is noise rather than an upgrade.
DEFAULT_MIN_GAIN = 0.50
# A player this widely owned who is somehow available is worth calling out.
NOTABLE_OWNERSHIP = 40.0


@dataclass
class Swap:
    add: Player
    drop: Player
    net: float
    deltas: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    legal: bool = True

    @property
    def gains(self) -> list[tuple[str, float]]:
        return sorted(
            [(c, d) for c, d in self.deltas.items() if d > 0.08],
            key=lambda item: -item[1],
        )

    @property
    def losses(self) -> list[tuple[str, float]]:
        return sorted([(c, d) for c, d in self.deltas.items() if d < -0.08], key=lambda i: i[1])


@dataclass
class WaiverReport:
    swaps: list[Swap] = field(default_factory=list)
    watchlist: list[Player] = field(default_factory=list)
    stash_candidates: list[Player] = field(default_factory=list)
    replacement_level: float = 0.0
    notes: list[str] = field(default_factory=list)


def _sample_note(player: Player) -> str | None:
    """Flag thin samples so a 2-game eruption isn't read as a breakout."""
    last7 = player.window("last7")
    if last7.usable and last7.games <= 1:
        return f"only {last7.games:.0f} game in the last week - tiny sample"
    return None


def _describe(player: Player, schedule: Schedule, today: dt.date) -> list[str]:
    """Context lines that explain where a player's value comes from."""
    reasons: list[str] = []
    last7, last30 = player.window("last7"), player.window("last30")

    if player.trend > 0.75:
        reasons.append(f"trending up (+{player.trend:.2f} z vs season baseline)")
    elif player.trend < -0.75:
        reasons.append(f"trending down ({player.trend:.2f} z vs season baseline)")

    if last30.usable and last30.per_game.get("MIN"):
        minutes = last30.per_game["MIN"]
        recent_minutes = last7.per_game.get("MIN") if last7.usable else None
        if recent_minutes and recent_minutes - minutes >= 4:
            reasons.append(
                f"minutes up {recent_minutes - minutes:+.1f} ({minutes:.1f} -> "
                f"{recent_minutes:.1f}) - role may be changing"
            )
        elif recent_minutes and minutes - recent_minutes >= 4:
            reasons.append(
                f"minutes down {recent_minutes - minutes:+.1f} ({minutes:.1f} -> "
                f"{recent_minutes:.1f})"
            )

    if schedule.available and player.team:
        games = schedule.game_count(player.team, today, 7)
        reasons.append(f"{games} game{'s' if games != 1 else ''} in the next 7 days")

    if player.pct_owned >= NOTABLE_OWNERSHIP and not player.on_my_roster:
        reasons.append(f"{player.pct_owned:.0f}% owned league-wide")

    return reasons


def recommend(
    my_players: list[Player],
    available: list[Player],
    slots: list[str],
    schedule: Schedule | None = None,
    today: dt.date | None = None,
    min_gain: float = DEFAULT_MIN_GAIN,
    max_candidates: int = 12,
    max_swaps: int = 8,
) -> WaiverReport:
    """Rank the best add/drop pairs, plus players to watch but not act on."""
    schedule = schedule or Schedule(available=False)
    today = today or dt.date.today()
    report = WaiverReport()

    if schedule.available is False:
        report.notes.append(
            "Schedule feed unavailable, so games-in-next-7-days context is missing."
        )

    pickups = [p for p in available if p.blended_value or p.windows]
    pickups.sort(key=lambda p: -p.blended_value)
    candidates = [p for p in pickups if not p.is_out][:max_candidates]

    # Replacement level: what you can get off the wire without trying hard.
    tier = [p.blended_value for p in pickups[:5]]
    report.replacement_level = sum(tier) / len(tier) if tier else 0.0

    # Injured-but-good available players are stashes, not starts.
    report.stash_candidates = [
        p
        for p in pickups
        if p.is_out and p.value.get("season", 0.0) > report.replacement_level
    ][:5]

    droppable = sorted(my_players, key=lambda p: p.blended_value)
    roster_ids = {p.pid for p in my_players}

    seen_adds: set[int] = set()
    for add in candidates:
        if add.pid in roster_ids or add.pid in seen_adds:
            continue
        best: Swap | None = None
        for drop in droppable:
            net = add.blended_value - drop.blended_value
            if net <= 0:
                continue

            after = [p for p in my_players if p.pid != drop.pid] + [add]
            legal = fillable_slots(after, slots) >= fillable_slots(my_players, slots)

            swap = Swap(
                add=add,
                drop=drop,
                net=net,
                deltas=category_deltas(add, drop),
                legal=legal,
            )
            if best is None or net > best.net:
                best = swap

        if best is None or best.net < min_gain:
            continue

        best.reasons = _describe(add, schedule, today)
        note = _sample_note(add)
        if note:
            best.warnings.append(f"{add.name}: {note}")
        if add.is_questionable:
            best.warnings.append(f"{add.name} is listed {add.status}")
        if add.needs_waiver_claim:
            best.warnings.append(
                "on waivers - needs a claim (FAAB/priority), not a free add"
            )
        if not best.legal:
            best.warnings.append(
                f"dropping {best.drop.name} would leave a lineup slot you "
                "cannot legally fill - check position coverage"
            )
        if best.drop.trend > 0.75:
            best.warnings.append(
                f"{best.drop.name} is trending up (+{best.drop.trend:.2f} z) - "
                "you may be selling low"
            )
        if best.drop.is_out and best.drop.value.get("season", 0.0) > add.blended_value:
            best.warnings.append(
                f"{best.drop.name} is {best.drop.status} but was better than "
                f"{add.name} when healthy - consider stashing instead"
            )

        seen_adds.add(add.pid)
        report.swaps.append(best)

    report.swaps.sort(key=lambda s: -s.net)
    report.swaps = report.swaps[:max_swaps]

    chosen = {s.add.pid for s in report.swaps}
    report.watchlist = [
        p for p in pickups[: max_candidates + 8] if p.pid not in chosen and not p.is_out
    ][:8]
    return report


def format_delta_line(deltas: dict[str, float], limit: int = 4) -> str:
    """Compact 'STL +0.8, 3PTM +0.4, AST -0.5' summary of a swap."""
    ordered = sorted(deltas.items(), key=lambda item: -abs(item[1]))[:limit]
    parts = []
    for cat, value in ordered:
        if abs(value) < 0.08:
            continue
        arrow = "+" if value > 0 else ""
        hint = " (fewer TO)" if cat in NEGATIVE_CATS and value > 0 else ""
        parts.append(f"{cat} {arrow}{value:.2f}{hint}")
    return ", ".join(parts) if parts else "no meaningful category change"
