"""Command line interface.

    python -m fantasy auth            # one-time Yahoo consent
    python -m fantasy leagues         # find your league key
    python -m fantasy init            # save league/team into .env
    python -m fantasy run             # the full report
    python -m fantasy waivers         # just add/drop
    python -m fantasy lineup          # just start/sit
    python -m fantasy probe           # verify stat parsing against your league
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import analysis as analysis_mod
from . import report as report_mod
from .client import FantasyClient
from .config import REPORTS_DIR, WindowWeights, set_env_values
from .players import WINDOW_ORDER


def _parse_date(value: str | None) -> dt.date:
    if not value or value == "today":
        return dt.date.today()
    if value == "tomorrow":
        return dt.date.today() + dt.timedelta(days=1)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"Bad --date {value!r}; use YYYY-MM-DD, today, or tomorrow.")


def _client(args: argparse.Namespace) -> FantasyClient:
    return FantasyClient(
        league_key=getattr(args, "league", None),
        use_cache=not getattr(args, "fresh", False),
    )


def _run_analysis(args: argparse.Namespace) -> analysis_mod.Analysis:
    return analysis_mod.run(
        _client(args),
        date=_parse_date(getattr(args, "date", None)),
        mode=args.mode,
        weights=WindowWeights.parse(args.weights),
        candidate_limit=args.candidates,
        min_gain=args.min_gain,
        include_questionable=not args.strict_health,
        skip_schedule=args.no_schedule,
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_auth(args: argparse.Namespace) -> int:
    """Interactive Yahoo consent, then prove it worked with a real API call.

    Must be run from a real terminal: Yahoo's installed-app flow has no
    callback server, so the library opens a browser and waits for you to paste
    back the verifier code it shows.
    """
    from . import auth as auth_mod

    if not sys.stdin.isatty() and not args.code:
        print(
            "This command needs an interactive terminal (it prompts you to paste\n"
            "the URL Yahoo redirects you to). In Claude Code, prefix it with '!':\n"
            "    ! .venv/Scripts/python.exe -m fantasy auth\n"
            "Or pass what you copied directly:\n"
            "    python -m fantasy auth --code '<pasted URL or code>'",
            file=sys.stderr,
        )
        return 2

    auth_mod.login(open_browser=not args.no_browser, pasted=args.code)

    print("\nToken acquired. Verifying with a live API call...")
    client = _client(args)
    try:
        leagues = client.my_leagues()
    except Exception as exc:
        print(f"\nAuthorized, but the test call failed: {exc}", file=sys.stderr)
        print("Run `python -m fantasy doctor` for details.", file=sys.stderr)
        return 1

    readable = [lg for lg in leagues if not lg.get("error")]
    print(f"Working. Found {len(readable)} NBA league(s) on this account:\n")
    for league in readable:
        print(
            f"  {league['league_key']:<20} {league.get('season', '?'):<6} "
            f"{league.get('name', '')}"
        )
    print("\nNext: python -m fantasy init --league <key>")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check each setup stage and say exactly what to fix.

    Safe to run any time; it never starts the interactive consent flow.
    """
    from .config import ENV_FILE, OAUTH_FILE, env, read_json

    ok = True

    def check(label: str, passed: bool, detail: str = "", fix: str = "") -> bool:
        nonlocal ok
        print(f"[{'PASS' if passed else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
        if not passed:
            ok = False
            if fix:
                for line in fix.splitlines():
                    print(f"       {line}")
        return passed

    print("Yahoo fantasy advisor - setup check\n")

    check(
        ".env file exists",
        ENV_FILE.exists(),
        str(ENV_FILE),
        fix="cp .env.example .env   (then fill in the two Yahoo values)",
    )
    client_id = env("YAHOO_CLIENT_ID")
    client_secret = env("YAHOO_CLIENT_SECRET")
    have_creds = check(
        "YAHOO_CLIENT_ID set",
        bool(client_id),
        f"...{client_id[-6:]}" if client_id else "",
        fix="Create an app at https://developer.yahoo.com/apps/create/\n"
        "Application Type MUST be 'Installed Application'\n"
        "API Permissions: tick 'Fantasy Sports' -> Read",
    )
    have_creds = check("YAHOO_CLIENT_SECRET set", bool(client_secret)) and have_creds

    if not have_creds:
        print("\nFix the above, then re-run: python -m fantasy doctor")
        return 1

    tokens = read_json(OAUTH_FILE, {}) or {}
    has_token = isinstance(tokens, dict) and bool(tokens.get("access_token"))
    check(
        "Yahoo token cached",
        has_token,
        str(OAUTH_FILE) if has_token else "no token yet",
        fix="Run in an interactive terminal:\n"
        "  .venv/Scripts/python.exe -m fantasy auth",
    )
    if not has_token:
        return 1

    stale = isinstance(tokens, dict) and tokens.get("consumer_key") != client_id
    check(
        "Cached token matches current client id",
        not stale,
        "credentials rotated" if stale else "",
        fix="Delete .secrets/oauth2.json and re-run `python -m fantasy auth`.",
    )

    client = FantasyClient(league_key=getattr(args, "league", None), use_cache=False)
    try:
        leagues = [lg for lg in client.my_leagues() if not lg.get("error")]
        check("Live API call", True, f"{len(leagues)} NBA league(s) visible")
        for league in leagues:
            print(f"       {league['league_key']:<20} {league.get('season','?'):<6} "
                  f"{league.get('name','')}")
    except Exception as exc:
        check(
            "Live API call",
            False,
            str(exc)[:200],
            fix="If this is a 401, the token is bad: delete .secrets/oauth2.json\n"
            "and re-run `python -m fantasy auth`.",
        )
        return 1

    league_key = env("LEAGUE_KEY")
    if not check(
        "LEAGUE_KEY saved in .env",
        bool(league_key),
        league_key or "",
        fix="python -m fantasy init --league <key from above>",
    ):
        return 1

    try:
        configured = FantasyClient(league_key=league_key, use_cache=False)
        cats = configured.categories()
        slots = configured.roster_slots()
        roster = configured.my_roster()
        check("League readable", True, f"{len(cats)} categories, {len(slots)} slots")
        print(f"       categories: {', '.join(cats)}")
        check(
            "Roster readable",
            bool(roster),
            f"{len(roster)} players",
            fix="An empty roster usually means LEAGUE_KEY points at an old season.",
        )
    except Exception as exc:
        check("League readable", False, str(exc)[:200])
        return 1

    print("\nAll good." if ok else "\nSome checks failed; see fixes above.")
    return 0 if ok else 1


def cmd_leagues(args: argparse.Namespace) -> int:
    for league in _client(args).my_leagues():
        if league.get("error"):
            print(f"{league['league_key']:<20} (unreadable: {league['error']})")
            continue
        print(
            f"{league['league_key']:<20} {league.get('season', '?'):<6} "
            f"{league.get('num_teams', '?'):>2} teams  "
            f"{league.get('scoring', ''):<12} {league.get('name', '')}"
        )
    print("\nPick the key for this season, then: python -m fantasy init --league <key>")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    client = _client(args)
    if not client.league_key:
        leagues = [lg for lg in client.my_leagues() if not lg.get("error")]
        if not leagues:
            raise SystemExit("No NBA leagues found for this account.")
        # Most recent season wins; that's almost always the active league.
        leagues.sort(key=lambda lg: str(lg.get("season", "")), reverse=True)
        client.league_key = leagues[0]["league_key"]
        print(f"Using {client.league_key} ({leagues[0].get('name')})")

    team_key = client.my_team_key()
    settings = client.settings()
    set_env_values({"LEAGUE_KEY": client.league_key, "TEAM_KEY": team_key})
    print(f"Saved to .env:\n  LEAGUE_KEY={client.league_key}\n  TEAM_KEY={team_key}")
    print(f"League: {settings.get('name')} ({settings.get('num_teams')} teams)")
    print(f"Categories: {', '.join(client.categories())}")
    print(f"Startable slots: {', '.join(client.roster_slots())}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    if args.json:
        print(json.dumps(report_mod.to_dict(result), indent=2))
    else:
        report_mod.print_full(result)
    if args.save:
        path = report_mod.save_markdown(result, REPORTS_DIR)
        if not args.json:
            report_mod.console.print(f"\n[green]Saved[/green] {path}")
    return 0


def cmd_waivers(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    if args.json:
        payload = report_mod.to_dict(result)
        print(json.dumps(
            {k: payload[k] for k in ("date", "mode", "suggestions", "watchlist",
                                     "stashes", "replacement_level", "notes")},
            indent=2,
        ))
    else:
        report_mod.print_header(result)
        report_mod.print_waivers(result)
        report_mod.print_footer(result)
    if args.save:
        report_mod.save_markdown(result, REPORTS_DIR)
    return 0


def cmd_lineup(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    if args.json:
        payload = report_mod.to_dict(result)
        print(json.dumps({"date": payload["date"], "lineup": payload["lineup"]}, indent=2))
    else:
        report_mod.print_header(result)
        report_mod.print_lineup(result)
        report_mod.print_footer(result)
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    if args.json:
        payload = report_mod.to_dict(result)
        print(json.dumps(
            {k: payload[k] for k in ("roster", "team_profile", "league")}, indent=2
        ))
    else:
        report_mod.print_header(result)
        report_mod.print_roster(result)
        report_mod.print_profile(result)
        report_mod.print_footer(result)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Confirm the stat plumbing against the live league.

    Worth running once after setup: it shows exactly which stat fields Yahoo
    returns for your league, which is how you'd catch a wrong stat id in
    statmap.py (symptom: missing GP or FGM/FGA, so percentages go unvalued).
    """
    client = _client(args)
    roster = client.my_roster()
    print(f"League: {client.league_key}  team: {client.my_team_key()}")
    print(f"Categories: {', '.join(client.categories())}")
    print(f"Startable slots: {', '.join(client.roster_slots())}")
    print(f"\nRoster rows: {len(roster)}")
    if not roster:
        print("Empty roster - is LEAGUE_KEY pointing at the current season?")
        return 1
    print("First roster row:")
    print(json.dumps(roster[0], indent=2, default=str))

    sample_ids = [int(r["player_id"]) for r in roster[:3]]
    for window in WINDOW_ORDER:
        stats = client.stats(sample_ids, window)
        print(f"\n--- {window} ---")
        for pid, row in stats.items():
            print(f"  {pid}: {json.dumps(row, default=str)}")
        missing = [
            field
            for field in ("GP", "FGM", "FGA", "FTM", "FTA")
            if not any(field in row for row in stats.values())
        ]
        if missing:
            print(f"  [!] missing fields: {', '.join(missing)}")
            print("      Percentages fall back to unvalued; check statmap.py ids.")
        else:
            print("  all fields needed for 9-cat valuation are present")

    avail = client.available_players("A")
    print(f"\nAvailable pool: {len(avail)} players")
    if avail:
        print("First available row:")
        print(json.dumps(avail[0], indent=2, default=str))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fantasy",
        description="Yahoo 9-cat fantasy basketball advisor.",
    )
    parser.add_argument("--league", help="league key, e.g. 466.l.12345 (default: .env)")
    parser.add_argument(
        "--fresh", action="store_true", help="bypass the local cache and refetch"
    )
    sub = parser.add_subparsers(dest="command")

    def add_analysis_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("--date", help="YYYY-MM-DD, today, or tomorrow")
        target.add_argument(
            "--mode",
            default="balanced",
            choices=["balanced", "needs"],
            help="balanced = equal categories; needs = tilt toward winnable ones",
        )
        target.add_argument(
            "--weights",
            help="window weights 'last7,last30,season' (default 0.4,0.4,0.2)",
        )
        target.add_argument(
            "--candidates",
            type=int,
            default=60,
            help="how many wire players to pull stats for (default 60)",
        )
        target.add_argument(
            "--min-gain",
            type=float,
            default=0.50,
            help="smallest net z gain worth suggesting (default 0.50)",
        )
        target.add_argument(
            "--strict-health",
            action="store_true",
            help="treat GTD/DTD players as unstartable",
        )
        target.add_argument(
            "--no-schedule",
            action="store_true",
            help="skip the ESPN schedule lookup",
        )
        target.add_argument("--json", action="store_true", help="machine-readable output")
        target.add_argument(
            "--save", action="store_true", help="also write a markdown report"
        )

    for name, handler, help_text in [
        ("run", cmd_run, "full report: waivers, lineup, roster, categories"),
        ("waivers", cmd_waivers, "add/drop suggestions only"),
        ("lineup", cmd_lineup, "start/sit for a date"),
        ("roster", cmd_roster, "your roster with valuations"),
    ]:
        child = sub.add_parser(name, help=help_text)
        add_analysis_flags(child)
        child.set_defaults(func=handler)

    auth_parser = sub.add_parser("auth", help="run the one-time Yahoo OAuth consent")
    auth_parser.add_argument(
        "--code",
        help="the redirected URL (or bare code) from Yahoo, if you already have it",
    )
    auth_parser.add_argument(
        "--no-browser", action="store_true", help="print the URL instead of opening it"
    )
    auth_parser.set_defaults(func=cmd_auth)

    for name, handler, help_text in [
        ("doctor", cmd_doctor, "check setup and report exactly what to fix"),
        ("leagues", cmd_leagues, "list your NBA leagues"),
        ("init", cmd_init, "save league/team keys to .env"),
        ("probe", cmd_probe, "verify stat parsing against your league"),
    ]:
        child = sub.add_parser(name, help=help_text)
        child.set_defaults(func=handler)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # Bare invocation does the useful thing.
        args = parser.parse_args(["run", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
