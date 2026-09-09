import datetime
import json
import os
import tempfile
import unittest

from pipeline import grade_history, weeks
from pipeline.match_games import Match, match_all, normalize
from pipeline.margin_model import parse_ladder
from pipeline.tests.factories import logistic_event

UTC = datetime.timezone.utc


class TestNormalization(unittest.TestCase):
    def test_state_abbreviation_matches_cfbd(self):
        for kalshi, cfbd_name in [("Ohio St.", "Ohio State"), ("Boise St.", "Boise State"),
                                  ("NC St.", "NC State"), ("Appalachian St.", "Appalachian State")]:
            self.assertEqual(normalize(kalshi), normalize(cfbd_name), kalshi)

    def test_ampersand_and_accents(self):
        self.assertEqual(normalize("Texas A&M"), normalize("Texas A&M"))
        self.assertEqual(normalize("San Jose St."), normalize("San José State"))

    def test_miami_schools_do_not_collide(self):
        """Stripping the parenthetical would merge two different schools."""
        self.assertEqual(normalize("Miami (FL)"), normalize("Miami"))
        self.assertNotEqual(normalize("Miami (FL)"), normalize("Miami (OH)"))


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.ladder = parse_ladder(logistic_event())   # Rutgers at Boston College

    def test_matches_and_orients_the_line(self):
        lines = [{"home_team": "Boston College", "away_team": "Rutgers",
                  "start_date": "2026-09-12T20:00:00Z", "home_favored_by": 6.5, "books": []}]
        matches, unmatched = match_all([self.ladder], [], lines)
        self.assertEqual(unmatched, [])
        self.assertFalse(matches[0].flipped)
        self.assertEqual(matches[0].home_favored_by, 6.5)

    def test_flipped_source_has_its_sign_corrected(self):
        """If CFBD lists the teams the other way round, the line must be
        re-oriented to the ladder's home team rather than silently inverted."""
        lines = [{"home_team": "Rutgers", "away_team": "Boston College",
                  "start_date": "2026-09-12T20:00:00Z", "home_favored_by": 6.5, "books": []}]
        matches, _ = match_all([self.ladder], [], lines)
        self.assertTrue(matches[0].flipped)
        self.assertEqual(matches[0].home_favored_by, -6.5)

    def test_far_off_date_does_not_match(self):
        lines = [{"home_team": "Boston College", "away_team": "Rutgers",
                  "start_date": "2026-11-01T20:00:00Z", "home_favored_by": 6.5, "books": []}]
        _, unmatched = match_all([self.ladder], [], lines)
        self.assertEqual(len(unmatched), 1)

    def test_unmatched_game_still_survives(self):
        matches, unmatched = match_all([self.ladder], [], [])
        self.assertEqual(len(matches), 1)
        self.assertIsNone(matches[0].home_favored_by)
        self.assertEqual(len(unmatched), 1)


class TestWeeks(unittest.TestCase):
    def test_week_key_is_the_saturday(self):
        for day in range(7, 13):                      # Mon 7th .. Sat 12th Sept 2026
            when = datetime.datetime(2026, 9, day, 18, 0, tzinfo=UTC)
            self.assertEqual(weeks.week_key(when), "2026-09-12")

    def test_deadline_is_thursday_noon_eastern(self):
        kickoff = datetime.datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
        deadline = weeks.decision_deadline(kickoff).astimezone(weeks.ET)
        self.assertEqual(deadline.weekday(), 3)       # Thursday
        self.assertEqual(deadline.hour, 12)
        self.assertLess(deadline, kickoff.astimezone(weeks.ET))

    def test_early_kickoff_deadline_never_follows_the_game(self):
        """A Wednesday game must lock at kickoff, not at Thursday noon."""
        kickoff = datetime.datetime(2026, 9, 9, 23, 0, tzinfo=UTC)
        self.assertLessEqual(weeks.decision_deadline(kickoff), kickoff)


def _pick(side, line, margin):
    return {"side": side, "team": "x", "line": line, "estimate": margin,
            "edge": margin - line, "p_cover": 0.6, "p_push": 0.0,
            "confidence": "solid" if side else "no-play", "reason": ""}


def _payload(margin, line, side, kickoff, game_id="G1"):
    return {
        "week_key": "2026-09-12", "season": 2026,
        "games": [{
            "id": game_id, "title": "Rutgers vs Boston College",
            "home_team": "Boston College", "away_team": "Rutgers",
            "kickoff": kickoff.isoformat(), "implied_margin": margin,
            "blended_margin": margin, "band": 0.3, "margin_low": margin - 0.15,
            "margin_high": margin + 0.15, "tier": "A", "open_interest": 20000,
            "fraction_traded": 0.9, "kalshi_weight": 1.0,
            "vegas_home_favored_by": line, "master_margin": margin,
            "pick": _pick(side, line, margin),
            "picks": {
                "master": _pick(side, line, margin),
                # the ladder lens deliberately takes the OTHER side here, so the
                # test can prove the four records are graded independently
                "kalshi_spread": _pick("away" if side == "home" else "home", line, margin),
                "kalshi_ml": _pick(side, line, margin),
                "sp_plus": _pick(None, line, margin),
            },
        }],
    }


class TestLockingAndGrading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.kickoff = datetime.datetime(2026, 9, 12, 23, 0, tzinfo=UTC)

    def _record(self, margin, at):
        grade_history.record(_payload(margin, 3.0, "home", self.kickoff),
                             history_dir=self.dir, now=at)
        return grade_history.load_week("2026-09-12", self.dir)["games"]["G1"]

    def test_decision_lock_freezes_after_the_deadline(self):
        """The headline record must describe the tool as actually used, so a
        later, better-informed snapshot may not overwrite it."""
        before = datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        entry = self._record(5.0, before)
        self.assertAlmostEqual(entry["decision"]["blended_margin"], 5.0)

        after = datetime.datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        entry = self._record(9.0, after)
        self.assertAlmostEqual(entry["decision"]["blended_margin"], 5.0,
                               msg="decision lock was overwritten after the deadline")
        self.assertAlmostEqual(entry["closing"]["blended_margin"], 9.0)

    def test_closing_lock_freezes_at_kickoff(self):
        self._record(5.0, datetime.datetime(2026, 9, 11, 12, 0, tzinfo=UTC))
        entry = self._record(30.0, self.kickoff + datetime.timedelta(hours=3))
        self.assertAlmostEqual(entry["closing"]["blended_margin"], 5.0)

    def test_grading_marks_covers_and_pushes(self):
        self._record(5.0, datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC))
        week = grade_history.load_week("2026-09-12", self.dir)

        graded = grade_history.apply_results(dict(week), {"G1": 10.0})
        self.assertTrue(graded["games"]["G1"]["result"]["decision_correct"])

        graded = grade_history.apply_results(dict(week), {"G1": -2.0})
        self.assertFalse(graded["games"]["G1"]["result"]["decision_correct"])

        graded = grade_history.apply_results(dict(week), {"G1": 3.0})
        self.assertIsNone(graded["games"]["G1"]["result"]["decision_correct"],
                          "an exact push is neither a win nor a loss")

    def test_each_lens_is_graded_independently(self):
        self._record(5.0, datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC))
        week = grade_history.load_week("2026-09-12", self.dir)
        graded = grade_history.apply_results(week, {"G1": 10.0})
        lenses = graded["games"]["G1"]["result"]["lenses"]
        self.assertTrue(lenses["master"]["decision_correct"])
        self.assertFalse(lenses["kalshi_spread"]["decision_correct"],
                         "the opposing lens pick must grade the other way")
        self.assertTrue(lenses["kalshi_ml"]["decision_correct"])
        self.assertIsNone(lenses["sp_plus"]["decision_correct"],
                          "a lens that made no pick has no result")

        grade_history.save_week(graded, self.dir)
        summary = grade_history.summarize(self.dir)
        by_key = {l["key"]: l["season"] for l in summary["lenses"]}
        self.assertEqual(by_key["master"]["wins"], 1)
        self.assertEqual(by_key["kalshi_spread"]["losses"], 1)
        self.assertEqual(by_key["sp_plus"]["total"], 0)

    def test_legacy_snapshot_without_lenses_still_grades(self):
        """The history file written before the lens tabs existed carries a single
        `pick`, which was the master pick. It must survive, not be discarded."""
        legacy = {"week_key": "2026-09-12", "games": {"OLD": {
            "title": "t", "home_team": "Boston College", "away_team": "Rutgers",
            "kickoff": self.kickoff.isoformat(),
            "decision": {"at": "2026-09-09T12:00:00+00:00", "tier": "A",
                         "pick": _pick("home", 3.0, 5.0)},
        }}}
        grade_history.save_week(legacy, self.dir)
        week = grade_history.load_week("2026-09-12", self.dir)
        graded = grade_history.apply_results(week, {"OLD": 10.0})
        self.assertTrue(graded["games"]["OLD"]["result"]["decision_correct"])
        self.assertTrue(graded["games"]["OLD"]["result"]["lenses"]["master"]["decision_correct"])

    def test_summary_excludes_ungraded_and_no_play_games(self):
        grade_history.record(_payload(5.0, 3.0, None, self.kickoff, "NOPLAY"),
                             history_dir=self.dir,
                             now=datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC))
        self._record(5.0, datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC))
        week = grade_history.load_week("2026-09-12", self.dir)
        grade_history.save_week(
            grade_history.apply_results(week, {"G1": 10.0, "NOPLAY": 10.0}), self.dir)

        summary = grade_history.summarize(self.dir)
        self.assertEqual(summary["season"]["decision"]["wins"], 1)
        self.assertEqual(summary["season"]["decision"]["total"], 1,
                         "a game with no pick must not inflate the denominator")


if __name__ == "__main__":
    unittest.main()
