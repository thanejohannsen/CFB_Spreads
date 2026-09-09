"""Single entrypoint for the scheduled job: fetch, lock, grade, summarise."""

from __future__ import annotations

import argparse
import datetime
import sys

from . import build_predictions, cfbd, grade_history

UTC = datetime.timezone.utc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-grade", action="store_true")
    ap.add_argument("--top-n", type=int, default=None)
    args = ap.parse_args()

    payload = build_predictions.build(top_n=args.top_n)
    print("wrote", build_predictions.write(payload))

    print("locked", grade_history.record(payload))

    if not args.skip_grade and cfbd.available():
        try:
            graded = grade_history.grade_week(payload["week_key"], payload["season"])
            if graded:
                done = sum(1 for e in graded["games"].values() if e.get("result"))
                print(f"graded {done} finished games in {payload['week_key']}")
        except Exception as exc:                            # noqa: BLE001
            print(f"grading skipped: {exc}", file=sys.stderr)

    summary = grade_history.summarize()
    print("wrote", grade_history.write_summary(summary))

    season = summary["season"]["decision"]
    print(f"record so far: {season['wins']}-{season['losses']} "
          f"across {summary['graded_games']} graded games")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
