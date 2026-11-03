# Yahoo 9-cat fantasy basketball advisor

Pulls your roster and the waiver wire from Yahoo, values everyone on
recent-form-weighted 9-category z-scores, and recommends add/drops and daily
lineups. Output is meant to be read and argued with — run it, then talk through
the suggestions.

## Setup

> ### Before anything else: Yahoo must approve your app
>
> **Since July 2026, the Fantasy Sports API is gated behind an approval
> program.** Creating a Yahoo app and completing OAuth is no longer enough —
> an unapproved app gets `403 "This application is not authorized to perform
> this action"` on *every* Fantasy endpoint, including ones that need no user
> context. OAuth will succeed and tokens will issue normally, which makes this
> look like a credentials bug. It isn't.
>
> Apply at **<https://sports.yahoo.com/developer/access/>**:
>
> - **Client ID**: paste the Client ID of the app you create in step 1 below.
>   (Existing Yahoo Developer Network apps are reused — you don't need a new
>   one.)
> - **Expected Users**: `Small (< 1,000 users)`
> - In the free-text/notes, say plainly what you're building, which Fantasy
>   data you need, and **that access is limited to personal, single-league
>   use** — the form explicitly asks applicants to state this.
>
> Then sign the **API Access and Use Agreement**, and confirm your Client ID at
> <https://sports.yahoo.com/developer/application-confirmation/>. Access is
> provisioned once Yahoo has both the signed agreement and your account info.
>
> No timeline is published. Yahoo warns that applications which are incomplete
> or not detailed enough are "closed without further correspondence," so write
> a real paragraph rather than one line.
>
> Other things worth knowing: the API is **read-only** (write access isn't
> offered); there's no published rate limit but Yahoo may throttle heavy use;
> and products are expected to display "Fantasy data provided by Yahoo Fantasy"
> linking back to Yahoo.
>
> Until approval lands, `python -m fantasy doctor` will report the 403 and
> everything else here will sit idle.

Three steps, and the first one is the only fiddly part.

### 1. Create a Yahoo app (one time, ~2 minutes)

Yahoo's Fantasy API requires OAuth, which means registering an app on your own
Yahoo account.

1. Sign in to Yahoo with **the account that owns your fantasy team**.
2. Go to <https://developer.yahoo.com/apps/create/>.
3. Fill in:
   - **Application Name**: anything, e.g. `fantasy-bball-advisor`
   - **Homepage URL**: leave blank
   - **Redirect URI(s)**: put anything (the form may require a value) — it
     isn't used. See the note below.
   - **OAuth Client Type**: **Confidential Client**. That's the option that
     issues a Client Secret, which this flow uses. "Public Client" is for
     PKCE apps with no secret and won't work here.
   - **API Permissions**: tick **Fantasy Sports - Read**. Leave **OpenID
     Connect Permissions** and **TW Auction** unchecked — the first is for
     using Yahoo as a login provider, the second is Yahoo Taiwan's auction
     API, and neither is needed to read your league.
4. Create the app. Yahoo shows a **Client ID** and **Client Secret**.

### 2. Save the credentials

```bash
cp .env.example .env     # already done if .env exists
```

Put the two values in `.env`:

```
YAHOO_CLIENT_ID=<the long Client ID>
YAHOO_CLIENT_SECRET=<the Client Secret>
```

### 3. Log in

```bash
.venv/Scripts/python.exe -m fantasy auth      # Windows
./.venv/bin/python -m fantasy auth            # macOS/Linux
```

It opens Yahoo's consent page. Approve access and Yahoo displays a short code
on screen — paste that at the prompt.

If you'd rather not use an interactive terminal, pass it directly:

```bash
python -m fantasy auth --code 'abc1234'
```

**Why `oob` and not a localhost callback:** Yahoo rejects `localhost`,
`127.0.0.1`, and any host you haven't proven you own, with
`error_description=invalid+redirect+uri` — at the authorize step, regardless of
what you typed into the app's Redirect URI field. So the usual
"spin up a local server on :8080" OAuth pattern is not available here. Yahoo's
out-of-band mode, where it shows you a code to copy, is the supported path for
a desktop script, and it's what `YAHOO_REDIRECT_URI=oob` selects. The tool
accepts either a bare code or a full redirect URL, so if you ever do register a
real https callback, it keeps working.

The token is cached in `.secrets/` (gitignored) and refreshes itself from then
on — you shouldn't need to do this again.

Then point it at your league:

```bash
python -m fantasy init          # auto-picks your current-season NBA league
python -m fantasy init --league 466.l.12345   # or name it explicitly
```

### Something not working?

```bash
python -m fantasy doctor
```

It checks every stage — credentials, token, live API call, league access,
roster read — and prints the specific fix for whatever failed.

## Daily use

```bash
python -m fantasy run              # the whole picture
python -m fantasy waivers          # just add/drop suggestions
python -m fantasy lineup           # just today's start/sit
python -m fantasy lineup --date tomorrow
python -m fantasy roster           # your roster, valued
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--mode needs` | Tilt valuation toward categories you can actually win, instead of weighting all nine equally |
| `--weights 0.6,0.3,0.1` | Reweight the stat windows (last 7, last 30, season). Default `0.4,0.4,0.2` |
| `--min-gain 0.25` | Lower the bar for what counts as a suggestion (default `0.50` z) |
| `--candidates 120` | Look deeper down the wire (default 60; costs more API calls) |
| `--save` | Also write a dated markdown report to `reports/` |
| `--json` | Machine-readable output |
| `--fresh` | Bypass the local cache |
| `--strict-health` | Treat GTD/DTD players as unstartable |

Responses are cached in `.cache/` (15 min for rosters, 1 hr for stats), so
re-running during a conversation is nearly free. Yahoo rate-limits aggressively,
which is why the cache exists.

## How players are valued

The headline number for every player is a blended 9-cat **z-score**: how many
standard deviations above or below the field they are, summed across categories.
Zero is an average rostered player in your league.

- **The comparison pool is every player rostered in your league**, not every
  player in the database. That's the population you actually compete against,
  which is what makes the number mean something.
- **Counting categories** (PTS, REB, AST, 3PTM, ST, BLK, TO) are per-game rates,
  z-scored. Turnovers are inverted so fewer is better.
- **Percentage categories are weighted by volume, not taken raw.** A player who
  goes 2-for-2 has a 100% FG but barely moves a weekly matchup; 10-for-16 moves
  it a lot. So FG% is scored as `FGM - FGA x league_FG%` — makes above what an
  average-efficiency player would have produced on the same attempts — and that
  impact gets z-scored. Same for FT%.
- **Three windows, recency-tilted.** Last 7 days, last 30, and season are each
  scored independently, then blended 40/40/20. A player heating up or losing a
  role shows up in the headline instead of being averaged away. The `Trend`
  column is last-7 value minus season value.
- Players with too few games in a window are excluded from that window rather
  than scored on noise, and the blend renormalizes over whatever windows they
  do have — so someone back from injury is judged on what they've played.

### What the suggestions account for

A raw z-score difference isn't a recommendation, so each suggested swap also
reports and flags:

- per-category deltas, so you can see *what* you'd gain and give up
- games in the next 7 days for both players
- minutes trending up or down (a role change vs. a hot shooting week)
- tiny samples (a 1-game eruption is labelled as such)
- injury status on both sides, and whether an injured player you'd drop was
  better than the pickup when healthy (stash vs. drop)
- whether the player is on **waivers** (needs a claim) or a **free agent**
- whether the swap would leave you unable to field a legal lineup — dropping
  your only center gets flagged

`--mode needs` additionally down-weights categories you've already locked up or
are realistically punting, since a marginal add changes fewer matchups there.
It's a heuristic and it says so; `balanced` is the default.

## Lineups

`lineup` solves the actual assignment problem (players to slots, maximizing
total value) with the Hungarian algorithm rather than filling positions greedily
— greedy gets it wrong exactly when it matters, like putting your only center in
a Util slot and stranding C. It only considers players who are healthy and whose
team has a game that day; the schedule comes from ESPN's public feed, and if
that's unreachable the report says so instead of quietly benching people.

## Development

```bash
python -m pytest              # 46 tests, no network or credentials needed
python -m fantasy probe       # dump raw Yahoo payloads for your league
```

`probe` is the thing to run if valuations look wrong — it shows exactly which
stat fields Yahoo returns, which is how you'd catch a bad stat id in
`src/fantasy/statmap.py` (symptom: missing GP or FGM/FGA, so percentages go
unvalued).

### Layout

| File | Role |
| --- | --- |
| `client.py` | Authenticated Yahoo access + disk cache |
| `statmap.py` | NBA stat-id names the SDK doesn't ship |
| `players.py` | Player model, per-game normalization |
| `valuation.py` | z-scores, volume-weighted percentages, category weights |
| `waivers.py` | Add/drop ranking and guardrails |
| `lineup.py` | Daily lineup optimizer |
| `schedule.py` | ESPN schedule lookup |
| `analysis.py` | Fetch-once orchestration |
| `report.py` | Terminal / JSON / markdown rendering |

## Caveats

- Valuation is descriptive, not predictive: it measures what players have done,
  with recent form weighted up. It has no injury-return projections, no
  schedule-strength adjustment, and no knowledge of why a role changed.
- Yahoo's `lastweek` / `lastmonth` windows are its own; in the first weeks of a
  season they overlap heavily with the full season.
- Nothing here writes to your Yahoo team. Every suggestion is yours to execute.
