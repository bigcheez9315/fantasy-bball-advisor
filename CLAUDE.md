# Working in this repo

Yahoo 9-cat fantasy basketball advisor. The user runs commands here and then
discusses the suggestions with Claude — the "discussion board" is the
conversation, so output is meant to be read and argued with, not just displayed.

## Running things

Always use the venv interpreter:

```bash
.venv/Scripts/python.exe -m fantasy run        # Windows (this machine)
```

- `python -m fantasy run --json` is the form to parse when reasoning about
  suggestions; the plain form is prettier for the user.
- `python -m fantasy doctor` diagnoses setup problems and prints fixes.
- `python -m pytest` — 39 tests, no network or credentials required.

`python -m fantasy auth` is **interactive** (Yahoo prompts for a pasted
verifier code) and cannot be run from a tool call. Ask the user to run it
themselves with a `!` prefix.

## Caching

Yahoo rate-limits hard, so responses are cached in `.cache/` (rosters 15 min,
stats 1 hr, league settings 24 hr). Re-running during a conversation is nearly
free. Use `--fresh` only when the user needs up-to-the-minute data — a full
uncached run is 30+ API calls.

## Things that are easy to get wrong

- **Stat ids**: the `yahoo_fantasy_api` SDK has no NBA static stat-id map, so it
  drops GP, FGM/FGA and FTM/FTA from responses. `statmap.py` injects them. If
  percentages stop being valued, run `python -m fantasy probe` — that's the
  symptom of a wrong id.
- **Percentage categories** are valued by volume (`FGM - FGA * league_FG%`), not
  raw percentage. Don't "simplify" this to a plain z-score of FG% — it makes
  2-for-2 players look elite. `tests/test_valuation.py` pins the behavior.
- **Window weights renormalize** per player over the windows they actually have,
  so a player with no 30-day sample isn't dragged toward zero.
- **Lineups use the Hungarian algorithm**, not greedy position filling. The test
  that pins this is `test_scarce_position_is_not_stranded` — greedy strands a C
  slot when your best player is the only center.
- The valuation pool is **rostered players league-wide**, not all players. That
  is deliberate: it's the population the user competes against.

## Conventions

- No secrets in git. `.env` and `.secrets/` are gitignored; `reports/` is not.
- Tests must stay runnable without network or credentials — `tests/test_pipeline.py`
  has a `FakeClient` returning Yahoo-shaped payloads. Extend it rather than
  adding tests that need a live league.
