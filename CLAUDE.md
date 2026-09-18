# Working in this repo

Three rules that are not obvious from the code, each one something that has already
gone wrong. Everything else is in the README.

## Merge to the default branch

The scheduled job commits there, GitHub Pages serves it, and the cron only runs there —
the README's *Scheduled updates* section explains why all three have to be the same
branch. **It is not called `main`.** Read the default branch rather than assuming a
name.

Develop on a work branch, but finish by merging into the default branch. Work left on a
work branch is work nobody can see: two changes once sat unmerged while the live site
served neither, and the symptom looked like a rendering bug on the page.

## Never rebuild `docs/data/` locally

There is no `CFBD_API_KEY` in a dev environment, and a keyless run still reaches Kalshi.
So `python -m pipeline.build_predictions` succeeds, writes a full-looking 49-game slate,
and silently blanks every Vegas line — which makes every pick a no-play. It looks like
the market went quiet, not like a broken run.

- Resolve any `docs/data/` merge conflict in favour of the scheduled job's copy.
- Never commit a locally built `current.json` or `cfbd_snapshot.json`.
- `grade_history.summarize()` and `pick_log()` are the exception: they read only stored
  history and touch no API, so regenerating `accuracy.json` and `picks.json` locally is
  safe and is how you check a records change before pushing.

`grade_history._keeps_its_line` defends the stored locks against this, but nothing
defends `current.json`.

## `docs/app.js` mirrors two Python modules

`pipeline/predict.py` (which pick) and `pipeline/execution.py` (where to place it) are
both re-implemented in the browser, so a hand-typed line is judged and priced by the
rules that grade the record. Change one side, change the other.

`node tools/conformance.js` fails the build when they drift — it loads the real
`app.js`, replays it over the committed `current.json`, and for the execution half asks
`python -m pipeline.execution` for the same answer. Run it before pushing any change to
either file.

A comment asserting two implementations agree is worth nothing unless something fails
when they stop, so if you add a third shared rule, extend the check with it.
