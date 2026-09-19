import collections
import datetime
import json
import os
import tempfile
import unittest

from pipeline import config, execution, grade_history, select_slate, weeks
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


class TestCfbdCache(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "cache.json")

    def test_entry_from_an_older_shape_is_stale(self):
        """A cached entry missing fields the current code expects must be
        refetched however recent it is. Without this, adding SP+ ratings meant
        every run inside the 8-hour window served an entry that predated them."""
        from pipeline import cfbd
        cfbd.save_cache({"2026-2": {
            "fetched_at": datetime.datetime.now(UTC).isoformat(),
            "games": [], "lines": [],          # no "schema", no "sp"
        }}, self.path)
        self.assertFalse(cfbd._fresh(cfbd.load_cache(self.path), "2026-2", 8))

    def test_current_shape_is_fresh(self):
        from pipeline import cfbd
        cfbd.save_cache({"2026-2": {
            "schema": cfbd.CACHE_SCHEMA,
            "fetched_at": datetime.datetime.now(UTC).isoformat(),
            "games": [], "lines": [], "sp": {},
        }}, self.path)
        self.assertTrue(cfbd._fresh(cfbd.load_cache(self.path), "2026-2", 8))

    def test_old_entry_expires_on_age_too(self):
        from pipeline import cfbd
        old = datetime.datetime.now(UTC) - datetime.timedelta(hours=9)
        cfbd.save_cache({"2026-2": {
            "schema": cfbd.CACHE_SCHEMA, "fetched_at": old.isoformat(),
            "games": [], "lines": [], "sp": {},
        }}, self.path)
        self.assertFalse(cfbd._fresh(cfbd.load_cache(self.path), "2026-2", 8))


class TestRecordsAndPickLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.kickoff = datetime.datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
        self.early = datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    def _store(self, game_id, tier, side="home"):
        grade_history.record(_payload(5.0, 3.0, side, self.kickoff, game_id, tier),
                             history_dir=self.dir, now=self.early)

    def test_a_keyless_run_cannot_blank_a_stored_line(self):
        """The bug: pipeline.update without CFBD_API_KEY still reaches Kalshi,
        so it builds a full slate with no Vegas line and every pick a no-play.
        That used to overwrite the week's locks, erasing the real line and the
        real picks -- one local run took a stored week from 30 priced locks to
        0, and the week would then grade as though the tool never had a view."""
        grade_history.record(_payload(5.0, 3.0, "home", self.kickoff),
                             history_dir=self.dir, now=self.early)
        stored = grade_history.load_week("2026-09-12", self.dir)["games"]["G1"]
        self.assertEqual(stored["decision"]["vegas_home_favored_by"], 3.0)
        self.assertEqual(stored["decision"]["picks"]["master"]["side"], "home")

        keyless = _payload(5.0, 3.0, "home", self.kickoff)
        game = keyless["games"][0]
        game["vegas_home_favored_by"] = None
        blank = {"side": None, "team": None, "line": 0.0, "estimate": 5.0, "edge": 0.0,
                 "p_cover": None, "p_push": 0.0, "confidence": "no-play",
                 "reason": "No Vegas line available"}
        game["pick"] = dict(blank)
        game["picks"] = {k: dict(blank) for k in game["picks"]}
        grade_history.record(keyless, history_dir=self.dir,
                             now=self.early + datetime.timedelta(hours=1))

        after = grade_history.load_week("2026-09-12", self.dir)["games"]["G1"]
        self.assertEqual(after["decision"]["vegas_home_favored_by"], 3.0,
                         "the keyless run must not blank the stored line")
        self.assertEqual(after["decision"]["picks"]["master"]["side"], "home",
                         "nor the picks made against it -- they were computed "
                         "from the line that just went missing")

    def test_an_ordinary_refresh_still_updates_the_lock(self):
        """The guard must not freeze a lock that a healthy run should refresh."""
        grade_history.record(_payload(5.0, 3.0, "home", self.kickoff),
                             history_dir=self.dir, now=self.early)
        grade_history.record(_payload(9.0, 6.5, "home", self.kickoff),
                             history_dir=self.dir,
                             now=self.early + datetime.timedelta(hours=1))
        after = grade_history.load_week("2026-09-12", self.dir)["games"]["G1"]
        self.assertEqual(after["decision"]["vegas_home_favored_by"], 6.5)
        self.assertEqual(after["decision"]["master_margin"], 9.0)

    def test_both_master_records_hold_every_tier_the_board_picks(self):
        """The bug: the headline kept S/A markets only, so a board showing six
        master picks sat above a record holding none of them. A record that
        cannot be reconciled against the board it is measuring is unreadable,
        however defensible the filter. Both master records now take every pick
        the Master tab makes; the refusal lives in the pick rules, not here."""
        self._store("TOP", "A")
        self._store("MID", "B")
        self._store("LOW", "C")
        week = grade_history.load_week("2026-09-12", self.dir)
        grade_history.save_week(
            grade_history.apply_results(week, {"TOP": 10.0, "MID": 10.0, "LOW": 10.0}),
            self.dir)

        summary = grade_history.summarize(self.dir)
        by_key = {r["key"]: r["season"] for r in summary["records"]}
        self.assertEqual(by_key["thursday_all"]["total"], 3)
        self.assertEqual(by_key["headline"]["total"], 3,
                         "the headline must hold the same picks the board shows")
        for rec in summary["records"]:
            self.assertIsNone(rec["tiers"], f"{rec['key']} must not filter on tier")
        self.assertNotIn("thursday_sa", by_key)

    def test_pick_log_matches_the_tally(self):
        self._store("TOP", "A")
        week = grade_history.load_week("2026-09-12", self.dir)
        grade_history.save_week(grade_history.apply_results(week, {"TOP": 10.0}), self.dir)

        summary = grade_history.summarize(self.dir)
        log = grade_history.pick_log(self.dir)
        for rec in summary["records"]:
            t = rec["season"]
            rows = [e for e in log["entries"]
                    if e["record"] == rec["key"]
                    and e["result"] not in ("pending", "live")]
            self.assertEqual(len(rows), t["wins"] + t["losses"],
                             f"{rec['key']}: log rows must match the record it expands")

    def test_ungraded_picks_appear_as_pending(self):
        """Otherwise the current week only shows up in hindsight, which is when
        the log is least useful."""
        self._store("TOP", "A")
        log = grade_history.pick_log(self.dir)
        rows = [e for e in log["entries"] if e["record"] == "thursday_all"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "pending")
        self.assertIsNone(rows[0]["home_margin"])

    def test_no_play_games_never_enter_the_log(self):
        self._store("NOPICK", "A", side=None)
        log = grade_history.pick_log(self.dir)
        self.assertEqual([e for e in log["entries"] if e["record"] == "thursday_all"], [])


class TestWeekSelection(unittest.TestCase):
    """The week must follow the games, not the clock.

    Kalshi lists the upcoming slate days ahead while a CFBD week runs through
    its own last game, so "which week is it now?" returns the previous week on a
    Monday -- which is exactly how a whole slate came back with no Vegas lines.
    """

    CAL = [
        {"season": 2026, "seasonType": "regular", "week": 2,
         "firstGameStart": "2026-09-08T00:00:00Z", "lastGameStart": "2026-09-14T23:00:00Z"},
        {"season": 2026, "seasonType": "regular", "week": 3,
         "firstGameStart": "2026-09-15T00:00:00Z", "lastGameStart": "2026-09-21T23:00:00Z"},
        {"season": 2026, "seasonType": "postseason", "week": 1,
         "firstGameStart": "2026-12-20T00:00:00Z", "lastGameStart": "2026-12-27T23:00:00Z"},
    ]

    def setUp(self):
        from pipeline import cfbd
        self.cfbd = cfbd
        self.path = os.path.join(tempfile.mkdtemp(), "cache.json")
        self._get = cfbd._get
        cfbd._get = lambda path, params: self.CAL if path == "/calendar" else []
        cfbd.calendar_weeks(2026, self.path)          # prime the cache

    def tearDown(self):
        self.cfbd._get = self._get

    def test_week_follows_the_games(self):
        self.assertEqual(
            self.cfbd.week_for_date(2026, datetime.date(2026, 9, 19)), 3,
            "a slate played Sep 19 belongs to week 3, whatever today is")

    def test_monday_of_the_previous_week_still_resolves_forward(self):
        """The exact failure: on Mon Sep 14 the old heuristic returned week 2
        because week 2 ran through a Monday game, while the board already held
        Sep 17-19 games."""
        self.assertEqual(self.cfbd.week_for_date(2026, datetime.date(2026, 9, 14)), 2)
        self.assertEqual(self.cfbd.week_for_date(2026, datetime.date(2026, 9, 17)), 3)

    def test_postseason_weeks_are_ignored(self):
        self.assertEqual(self.cfbd.week_for_date(2026, datetime.date(2026, 9, 16)), 3)

    def test_date_outside_every_week_snaps_to_the_nearest(self):
        self.assertEqual(self.cfbd.week_for_date(2026, datetime.date(2026, 8, 1)), 2)

    def test_calendar_is_cached_rather_than_refetched(self):
        """It used to be fetched every run -- around 780 calls a month at the
        current cadence, which alone would breach the free tier."""
        calls = []
        self.cfbd._get = lambda path, params: (calls.append(path), self.CAL)[1]
        self.cfbd.calendar_weeks(2026, self.path)
        self.assertEqual(calls, [], "a cached calendar must not hit the API")


class TestGradingSweep(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    def _week(self, week_key, kickoff, graded):
        entry = {"title": "t", "home_team": "Boston College", "away_team": "Rutgers",
                 "kickoff": kickoff.isoformat()}
        if graded:
            entry["result"] = {"home_margin": 7.0}
        grade_history.save_week({"week_key": week_key, "games": {"G": entry}}, self.dir)
        return grade_history.load_week(week_key, self.dir)

    def test_week_with_played_but_ungraded_games_is_swept(self):
        w = self._week("2026-09-12", datetime.datetime(2026, 9, 12, 23, tzinfo=UTC), False)
        self.assertTrue(grade_history.needs_grading(w, now=self.now))

    def test_fully_graded_week_is_left_alone(self):
        w = self._week("2026-09-12", datetime.datetime(2026, 9, 12, 23, tzinfo=UTC), True)
        self.assertFalse(grade_history.needs_grading(w, now=self.now))

    def test_games_not_yet_played_are_not_swept(self):
        w = self._week("2026-09-19", datetime.datetime(2026, 9, 19, 23, tzinfo=UTC), False)
        self.assertFalse(grade_history.needs_grading(w, now=self.now))

    def test_stale_week_is_abandoned(self):
        """Some games never grade at all, so the sweep has to stop eventually."""
        w = self._week("2026-07-04", datetime.datetime(2026, 7, 4, 23, tzinfo=UTC), False)
        self.assertFalse(grade_history.needs_grading(w, now=self.now))


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


def _payload(margin, line, side, kickoff, game_id="G1", tier="A"):
    return {
        "week_key": "2026-09-12", "season": 2026,
        "games": [{
            "id": game_id, "title": "Rutgers vs Boston College",
            "home_team": "Boston College", "away_team": "Rutgers",
            "kickoff": kickoff.isoformat(), "implied_margin": margin,
            "blended_margin": margin, "band": 0.3, "margin_low": margin - 0.15,
            "margin_high": margin + 0.15, "tier": tier, "open_interest": 20000,
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


class TestTheMoneylineLensIsGradedOnBothClocks(unittest.TestCase):
    """The master is graded at Thursday noon and again at T-1h, because "does a
    Wednesday read hold up?" deserves evidence. The moneyline lens now is too:
    it carries most of the master's weight on a tight game, and Kalshi's volume
    arrives late, so it is the signal most likely to answer differently.

    Nothing had to be computed for it -- apply_results already wrote
    `final_correct` for every lens -- so what these pin is that the two rows are
    genuinely independent and not the same tally printed twice."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.kickoff = datetime.datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
        # Thursday noon ET for that week is 2026-09-10 16:00 UTC; T-1h is
        # 2026-09-12 22:00 UTC. Between them only the final lock still moves.
        self.before_both = datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        self.between = datetime.datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    def _keys(self):
        return {key for key, _l, _lock, _lens, _t in grade_history.RECORDS}

    def test_the_lens_has_a_row_on_each_clock(self):
        keys = self._keys()
        self.assertIn("lens_kalshi_ml", keys)
        self.assertIn("lens_kalshi_ml_final", keys)
        by_key = {k: (lock, lens) for k, _l, lock, lens, _t in grade_history.RECORDS}
        self.assertEqual(by_key["lens_kalshi_ml"], ("decision", "kalshi_ml"))
        self.assertEqual(by_key["lens_kalshi_ml_final"], ("final", "kalshi_ml"))
        labels = [l for _k, l, _lock, _lens, _t in grade_history.RECORDS]
        self.assertEqual(len(labels), len(set(labels)),
                         "two rows sharing a label cannot be told apart on the page")

    def test_the_two_clocks_grade_independently(self):
        """The market moved between the deadlines, so the later lock took the
        other side. One row wins and the other loses on the same game."""
        grade_history.record(_payload(5.0, 3.0, "home", self.kickoff),
                             history_dir=self.dir, now=self.before_both)
        grade_history.record(_payload(1.0, 3.0, "away", self.kickoff),
                             history_dir=self.dir, now=self.between)
        week = grade_history.load_week("2026-09-12", self.dir)
        self.assertEqual(week["games"]["G1"]["decision"]["picks"]["kalshi_ml"]["side"], "home",
                         "Thursday noon had passed; that lock must be frozen")
        self.assertEqual(week["games"]["G1"]["final"]["picks"]["kalshi_ml"]["side"], "away")

        grade_history.save_week(
            grade_history.apply_results(week, {"G1": 10.0}), self.dir)   # home covers
        by_key = {r["key"]: r["season"] for r in grade_history.summarize(self.dir)["records"]}
        self.assertEqual((by_key["lens_kalshi_ml"]["wins"],
                          by_key["lens_kalshi_ml"]["losses"]), (1, 0))
        self.assertEqual((by_key["lens_kalshi_ml_final"]["wins"],
                          by_key["lens_kalshi_ml_final"]["losses"]), (0, 1))

    def test_a_pick_at_one_clock_only_reaches_that_record(self):
        """The later lock passing on a game it earlier liked must empty one row,
        not both -- otherwise the pair is one tally wearing two labels."""
        grade_history.record(_payload(5.0, 3.0, "home", self.kickoff),
                             history_dir=self.dir, now=self.before_both)
        grade_history.record(_payload(3.0, 3.0, None, self.kickoff),
                             history_dir=self.dir, now=self.between)
        rows = grade_history.pick_log(self.dir)["entries"]
        self.assertEqual([e["record"] for e in rows if e["record"].startswith("lens_kalshi_ml")],
                         ["lens_kalshi_ml"])

    def test_the_other_lenses_keep_a_single_clock(self):
        """Deliberate, not an oversight: the pair has to earn the extra column
        before the other two get one."""
        locks = collections.defaultdict(set)
        for _k, _l, lock, lens, _t in grade_history.RECORDS:
            locks[lens].add(lock)
        self.assertEqual(locks["kalshi_ml"], {"decision", "final"})
        self.assertEqual(locks["master"], {"decision", "final"})
        self.assertEqual(locks["kalshi_spread"], {"decision"})
        self.assertEqual(locks["sp_plus"], {"decision"})


class TestARecordTracksTheBoardUntilItLocks(unittest.TestCase):
    """The bug: the records showed a game only once its lock had fired, so on
    Thursday afternoon the Master tab showed six picks above a "Final (T-1h)"
    record showing none of them, for two days. Hiding the row and printing it
    as a settled pick are both wrong; the fix is to show it and say which it is.

    Each record therefore carries every pick the board shows from the moment it
    appears, marked live and dated by the moment it will freeze, and stops
    moving when its own clock runs out -- T-1h for the headline, noon ET for
    Thursday. Nothing ungraded reaches a tally either way."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.kickoff = datetime.datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
        self.before = datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        grade_history.record(_payload(5.0, 3.0, "home", self.kickoff),
                             history_dir=self.dir, now=self.before)
        self.entry = grade_history.load_week("2026-09-12", self.dir)["games"]["G1"]

    def _log(self, now):
        return grade_history.pick_log(self.dir, now=now)["entries"]

    def _row(self, now, record):
        return next((e for e in self._log(now) if e["record"] == record), None)

    def test_the_moments_are_the_ones_the_records_are_named_for(self):
        self.assertEqual(grade_history.lock_moment(self.entry, "decision"),
                         weeks.decision_deadline(self.kickoff))
        self.assertEqual(grade_history.lock_moment(self.entry, "final"),
                         weeks.final_lock(self.kickoff))

    def test_an_unfired_lock_is_listed_live_not_hidden(self):
        """The whole complaint: the board had picks and the record had nothing."""
        for key in ("thursday_all", "headline"):
            row = self._row(self.before, key)
            self.assertIsNotNone(row, f"{key} must show the pick the board is showing")
            self.assertEqual(row["result"], "live")
            self.assertFalse(row["locked"])
            self.assertEqual(
                row["locks_at"],
                (weeks.decision_deadline(self.kickoff) if key == "thursday_all"
                 else weeks.final_lock(self.kickoff)).isoformat(),
                "a live row has to say when it stops moving")

    def test_each_record_freezes_on_its_own_clock(self):
        after_thursday = weeks.decision_deadline(self.kickoff) + datetime.timedelta(minutes=1)
        self.assertTrue(self._row(after_thursday, "thursday_all")["locked"],
                        "noon ET has passed; the Thursday row is a commitment now")
        self.assertFalse(self._row(after_thursday, "headline")["locked"],
                         "T-1h is still ahead, so the headline row is still live")

        after_final = weeks.final_lock(self.kickoff) + datetime.timedelta(minutes=1)
        self.assertTrue(self._row(after_final, "headline")["locked"])

    def test_a_live_row_is_the_board_pick_and_follows_it(self):
        """A live row is a window onto the board, so when the board's pick moves
        before the lock, the row moves with it rather than showing a stale one."""
        self.assertEqual(self._row(self.before, "headline")["side"], "home")
        later = self.before + datetime.timedelta(hours=6)
        grade_history.record(_payload(1.0, 3.0, "away", self.kickoff),
                             history_dir=self.dir, now=later)
        self.assertEqual(self._row(later, "headline")["side"], "away")

    def test_nothing_ungraded_reaches_a_tally(self):
        """Showing the live board must not put an ungraded pick in the record."""
        summary = grade_history.summarize(self.dir, now=self.before)
        for rec in summary["records"]:
            self.assertEqual(rec["season"]["total"], 0, rec["key"])
        by_key = {r["key"]: r["open"] for r in summary["records"]}
        self.assertEqual(by_key["headline"], {"live": 1, "pending": 0},
                         "the row the board is showing has to be counted somewhere")

    def test_an_open_pick_moves_from_live_to_pending_at_its_lock(self):
        after_final = weeks.final_lock(self.kickoff) + datetime.timedelta(minutes=1)
        summary = grade_history.summarize(self.dir, now=after_final)
        by_key = {r["key"]: r["open"] for r in summary["records"]}
        self.assertEqual(by_key["headline"], {"live": 0, "pending": 1})

    def test_how_early_a_snapshot_was_taken_is_reported_once_it_is_frozen(self):
        """Michigan St. vs Notre Dame locked 12h before Thursday noon, because
        it had fallen out of the slate and record() stopped seeing it. The
        number has to reach the page or the record silently misrepresents
        itself."""
        after = weeks.decision_deadline(self.kickoff) + datetime.timedelta(minutes=1)
        entry = self._row(after, "thursday_all")
        expected = round((weeks.decision_deadline(self.kickoff)
                          - self.before).total_seconds() / 60)
        self.assertEqual(entry["minutes_before_lock"], expected)
        self.assertGreater(entry["minutes_before_lock"], 0)

    def test_a_live_row_reports_no_staleness_at_all(self):
        """A live snapshot is SUPPOSED to predate the moment it will lock at, so
        measuring the gap would flag every row on the board as stale."""
        row = self._row(self.before, "headline")
        self.assertIsNone(row["minutes_before_lock"])
        self.assertIsNone(row["minutes_before_kickoff"])
        self.assertIsNone(row["locked_at"])


class TestSlatePinning(unittest.TestCase):
    """The slate is the top N by open interest, recomputed every run, but a
    pick has to be watched until its lock fires. record() only ever sees games
    in the payload, so a picked game pushed out of the top N stops being
    re-evaluated: the board drops it while the record still grades it, and its
    open lock freezes holding a pick the tool no longer makes.

    Observed on Michigan St. vs Notre Dame: a master lean at 03:49, then out of
    the top 30 for every run before Thursday noon, so its decision lock froze
    twelve hours early on a pick the board had already stopped showing.

    Pinning is therefore for picks, not for attendance. The first attempt kept
    every game with any lock until its T-1h -- which before Saturday is every
    game that has touched the top N all week, 49 against a TOP_N of 30 -- and
    that is what these now pin against."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.kickoff = datetime.datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
        self.early = datetime.datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    def _lock(self, game_id, side="home"):
        """Lock one game. side=None means no lens picked it at all -- _payload
        deliberately has the ladder lens take the opposite side, so a pick-free
        game has to be built by blanking every side."""
        payload = _payload(5.0, 3.0, side, self.kickoff, game_id)
        if side is None:
            game = payload["games"][0]
            game["pick"]["side"] = None
            for pick in game["picks"].values():
                pick["side"] = None
        grade_history.record(payload, history_dir=self.dir, now=self.early)

    def _lock_lens(self, game_id, lens):
        """Lock a game that exactly one lens picks, to prove which lenses pin."""
        payload = _payload(5.0, 3.0, "home", self.kickoff, game_id)
        game = payload["games"][0]
        game["pick"]["side"] = None
        for key, pick in game["picks"].items():
            pick["side"] = "home" if key == lens else None
        grade_history.record(payload, history_dir=self.dir, now=self.early)

    def _ladders(self, ids_by_oi):
        return [parse_ladder(logistic_event(event=gid, oi=oi))
                for gid, oi in ids_by_oi]

    def test_a_locked_game_is_kept_even_when_it_falls_out_of_the_top_n(self):
        self._lock("KX-LOCKED")
        pinned = grade_history.locked_ids("2026-09-12", self.dir, now=self.early)
        self.assertIn("KX-LOCKED", pinned)

        ladders = self._ladders([("KX-BIG", 90000.0), ("KX-LOCKED", 100.0)])
        top1 = select_slate.select(ladders, top_n=1)
        self.assertEqual([l.event_ticker for l in top1], ["KX-BIG"],
                         "precondition: the locked game is outside the top N")

        kept = select_slate.select(ladders, top_n=1, pinned=pinned)
        self.assertEqual([l.event_ticker for l in kept], ["KX-BIG", "KX-LOCKED"])

    def test_pinning_never_duplicates_a_game_already_in_the_top_n(self):
        self._lock("KX-BIG")
        pinned = grade_history.locked_ids("2026-09-12", self.dir, now=self.early)
        ladders = self._ladders([("KX-BIG", 90000.0), ("KX-OTHER", 100.0)])
        kept = select_slate.select(ladders, top_n=2, pinned=pinned)
        self.assertEqual([l.event_ticker for l in kept], ["KX-BIG", "KX-OTHER"])

    def test_no_history_leaves_the_slate_exactly_as_it_was(self):
        ladders = self._ladders([("KX-BIG", 90000.0), ("KX-OTHER", 100.0)])
        self.assertEqual(select_slate.select(ladders, top_n=1, pinned=set()),
                         select_slate.select(ladders, top_n=1))

    def test_a_played_game_that_was_never_picked_stops_being_pinned(self):
        """Otherwise the slate only ever grows: every game that touched the top
        N this week would stay on the board until the week rolled over."""
        self._lock("KX-NOPLAY", side=None)
        after = self.kickoff + datetime.timedelta(hours=4)
        self.assertEqual(
            grade_history.locked_ids("2026-09-12", self.dir, now=after), set())

    def test_a_picked_game_stays_pinned_after_its_last_lock(self):
        """The record is still grading it, so the board must still show it."""
        self._lock("KX-PICKED", side="home")
        after = self.kickoff + datetime.timedelta(hours=4)
        self.assertIn("KX-PICKED",
                      grade_history.locked_ids("2026-09-12", self.dir, now=after))

    def test_a_no_play_is_not_pinned_even_before_its_final_lock(self):
        """This reverses an earlier rule, deliberately. Pinning every game with
        a lock until T-1h meant pinning every game that had touched the top N
        all week -- 49 against a TOP_N of 30 on a measured slate. A game the
        tool has no opinion on needs nothing from the record, and if it is
        inside the top N it is in the payload anyway."""
        self._lock("KX-NOPLAY", side=None)
        self.assertEqual(
            grade_history.locked_ids("2026-09-12", self.dir, now=self.early), set())

    def test_a_picked_game_is_pinned_so_the_pick_can_be_DROPPED(self):
        """The point of pinning, and the reason the narrow rule is not a
        regression: a picked game keeps being evaluated, so when the edge goes
        the open lock is overwritten with a no-play and the record loses the
        pick. Michigan St. vs Notre Dame is the case that was not -- it fell out
        of the slate holding a lean, stopped being looked at, and its Thursday
        lock froze on a snapshot twelve hours older than the deadline."""
        self._lock("KX-EDGY", side="home")
        self.assertIn("KX-EDGY",
                      grade_history.locked_ids("2026-09-12", self.dir, now=self.early))

        # the edge goes while the deadline is still ahead
        later = self.early + datetime.timedelta(hours=6)
        payload = _payload(3.0, 3.0, None, self.kickoff, "KX-EDGY")
        game = payload["games"][0]
        game["pick"]["side"] = None
        for pick in game["picks"].values():
            pick["side"] = None
        grade_history.record(payload, history_dir=self.dir, now=later)

        entry = grade_history.load_week("2026-09-12", self.dir)["games"]["KX-EDGY"]
        self.assertIsNone(entry["decision"]["picks"]["master"]["side"],
                          "the open lock must lose the pick, not keep it")
        self.assertEqual(grade_history.locked_ids("2026-09-12", self.dir, now=later), set(),
                         "and with no pick left there is nothing to hold it in the slate")

    def test_which_lenses_hold_a_game_in_the_slate(self):
        """SP+ is excluded on the evidence: it is a near-static power rating and
        changed side between locks on 24% of its picked games, against 74% for
        the moneyline and 100% for the master and the ladder. Re-evaluating a
        signal that does not move buys little, and SP+ alone dragged 17 of 19
        extra games onto a measured board."""
        for lens in ("master", "kalshi_spread", "kalshi_ml"):
            with self.subTest(lens=lens):
                self.assertIn(lens, grade_history.PINNING_LENSES)
        self.assertNotIn("sp_plus", grade_history.PINNING_LENSES)

        self._lock_lens("KX-SPONLY", "sp_plus")
        self._lock_lens("KX-ML", "kalshi_ml")
        pinned = grade_history.locked_ids("2026-09-12", self.dir, now=self.early)
        self.assertNotIn("KX-SPONLY", pinned, "an SP+ pick alone does not pin")
        self.assertIn("KX-ML", pinned)

    def test_the_rule_does_not_depend_on_the_clock(self):
        """The old rule turned on whether T-1h had passed. This one asks only
        whether a pick exists, so it answers the same at every moment."""
        self._lock("KX-PICKED", side="home")
        answers = {frozenset(grade_history.locked_ids("2026-09-12", self.dir, now=t))
                   for t in (self.early, self.kickoff,
                             self.kickoff + datetime.timedelta(days=3))}
        self.assertEqual(len(answers), 1)


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
        self.assertAlmostEqual(entry["final"]["blended_margin"], 9.0)

    def test_final_lock_freezes_one_hour_before_kickoff(self):
        """The headline lock sits at kickoff minus an hour, so a snapshot taken
        inside that final hour must not overwrite it."""
        entry = self._record(5.0, self.kickoff - datetime.timedelta(hours=3))
        self.assertAlmostEqual(entry["final"]["blended_margin"], 5.0)
        entry = self._record(30.0, self.kickoff - datetime.timedelta(minutes=30))
        self.assertAlmostEqual(entry["final"]["blended_margin"], 5.0)

    def test_final_lock_records_how_stale_it_is(self):
        """A missed cron run leaves the lock older than intended; without the
        gap a stale lock looks identical to a fresh one."""
        entry = self._record(5.0, self.kickoff - datetime.timedelta(minutes=75))
        self.assertEqual(entry["final"]["minutes_before_kickoff"], 75)

    def test_legacy_closing_lock_is_read_as_final(self):
        """The second lock used to sit at kickoff and be called `closing`."""
        legacy = {"week_key": "2026-09-12", "games": {"OLD": {
            "title": "t", "home_team": "Boston College", "away_team": "Rutgers",
            "kickoff": self.kickoff.isoformat(),
            "closing": {"at": "2026-09-12T22:00:00+00:00", "tier": "A",
                        "pick": _pick("home", 3.0, 5.0)},
        }}}
        grade_history.save_week(legacy, self.dir)
        graded = grade_history.apply_results(
            grade_history.load_week("2026-09-12", self.dir), {"OLD": 10.0})
        self.assertTrue(graded["games"]["OLD"]["result"]["final_correct"])

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


class TestExecutionSideMapping(unittest.TestCase):
    """The bug this guards: a home rung's YES is {margin > x} but an away rung's
    YES is {margin < x}, so reading the wrong one does not skew the answer, it
    prices the other team's bet. Measured live, getting it backwards flipped the
    recommended venue on two of two checkable games and turned a -3.66c row into
    a +5.33c one. All four combinations, one assertion each."""

    def test_all_four_combinations(self):
        self.assertTrue(execution.wants_yes(True, "home"),
                        "a home rung's YES is the home side covering")
        self.assertFalse(execution.wants_yes(True, "away"),
                         "the away side of a home rung is its NO")
        self.assertTrue(execution.wants_yes(False, "away"),
                        "an away rung's YES is the away side covering")
        self.assertFalse(execution.wants_yes(False, "home"),
                         "the home side of an away rung is its NO")

    def test_the_two_sides_cost_what_the_book_charges_for_them(self):
        """Buying the YES lifts the ask; buying the NO pays 1 - bid."""
        quote = {"bid": 0.40, "ask": 0.44, "bid_size": 500, "ask_size": 500,
                 "home_strike": True}
        home = {r["venue"]: r for r in
                execution.venues(quote, "home", 0.5)["rows"]}
        away = {r["venue"]: r for r in
                execution.venues(quote, "away", 0.5)["rows"]}
        self.assertAlmostEqual(home["kalshi_take"]["cost"], 0.44)
        self.assertAlmostEqual(away["kalshi_take"]["cost"], 0.60)   # 1 - 0.40
        self.assertAlmostEqual(home["kalshi_rest"]["cost"], 0.40)
        self.assertAlmostEqual(away["kalshi_rest"]["cost"], 0.56)   # 1 - 0.44

    def test_thresholds_are_positive_so_the_sign_carries_the_side(self):
        """`quotes` publishes a signed x and the page reads the side off its
        sign alone. That only works because Kalshi thresholds are positive --
        0 of 2,303 rungs across a live slate were at or below zero."""
        from pipeline.margin_model import parse_ladder
        ladder = parse_ladder(logistic_event())
        self.assertTrue(all(s.threshold > 0 for s in ladder.strikes))
        for s in ladder.strikes:
            x = s.threshold if s.abbrev == ladder.home_abbrev else -s.threshold
            self.assertEqual(x > 0, s.abbrev == ladder.home_abbrev)


class TestExecutionCost(unittest.TestCase):
    def test_maker_is_a_quarter_of_taker(self):
        self.assertAlmostEqual(execution.fee_rate(0.5, maker=True),
                               execution.fee_rate(0.5) * 0.25)

    def test_the_fee_peaks_at_the_coin_flip(self):
        """0.07 * p * (1-p) is largest at 50c -- 1.75c, the published maximum."""
        self.assertAlmostEqual(execution.fee_rate(0.5), 0.0175)
        self.assertGreater(execution.fee_rate(0.5), execution.fee_rate(0.2))
        self.assertGreater(execution.fee_rate(0.5), execution.fee_rate(0.9))

    def test_an_order_rounds_up_to_the_next_cent(self):
        """The rounding is a property of the ORDER, not the price: one contract
        at 22c is charged 2c, a thousand are charged 1.2c each. Which is why
        every cross-venue comparison uses the unrounded rate instead."""
        self.assertAlmostEqual(execution.order_fee(0.22, 1), 0.02)
        self.assertAlmostEqual(execution.order_fee(0.22, 1000) / 1000, 0.01202, places=5)
        # ...and both bracket the unrounded rate the venue comparison uses.
        self.assertLess(execution.fee_rate(0.22), execution.order_fee(0.22, 1))
        self.assertAlmostEqual(execution.fee_rate(0.22), 0.012012, places=6)

    def test_a_kalshi_price_is_its_own_break_even(self):
        """It settles at $1, so there is no odds conversion -- only the fee."""
        self.assertAlmostEqual(execution.break_even(0.50),
                               0.50 + execution.fee_rate(0.50))
        self.assertAlmostEqual(execution.american_break_even(-110), 110 / 210)
        self.assertAlmostEqual(execution.american_break_even(150), 100 / 250)


class TestExecutionVenues(unittest.TestCase):
    QUOTE = {"bid": 0.50, "ask": 0.52, "bid_size": 900, "ask_size": 900,
             "home_strike": True}

    def test_ranking_needs_no_model(self):
        """p_cover scales every edge by the same amount, so it cannot reorder
        the rows. That is what makes the recommendation model-free."""
        order = lambda p: [r["venue"] for r in
                           execution.venues(self.QUOTE, "home", p)["rows"]
                           if r["fillable"]]
        self.assertEqual(order(0.40), order(0.99))

    def test_the_decomposition_adds_back_up(self):
        """Any gap between two venues is cost-of-dealing plus disagreement, and
        only the first is a saving. If these stop summing, the box is double
        counting the tool's own signal as a discount."""
        v = execution.venues(self.QUOTE, "home", 0.55)
        rows = {r["venue"]: r for r in v["rows"]}
        gap = rows["book"]["break_even"] - rows["kalshi_rest"]["break_even"]
        dealing = rows["book"]["dealing"] - rows["kalshi_rest"]["dealing"]
        self.assertAlmostEqual(gap, dealing + v["disagreement"])

    def test_dealing_is_measured_against_each_venue_own_mid(self):
        """A -110/-110 book charges 2.38 points a side. Kalshi charges its half
        spread plus the fee -- and resting collects rather than pays."""
        v = execution.venues(self.QUOTE, "home", 0.55)
        rows = {r["venue"]: r for r in v["rows"]}
        self.assertAlmostEqual(rows["book"]["dealing"], 110 / 210 - 0.5)
        self.assertGreater(rows["kalshi_take"]["dealing"], 0)
        self.assertLess(rows["kalshi_rest"]["dealing"], 0)

    def test_a_dust_quote_is_not_a_price_you_can_take(self):
        """Kalshi seeds levels with ~0.02 contracts and reports them as top of
        book; every apparent crossed pair on a live slate sat on 0.01-0.04.
        Offering that as a venue is advice that cannot be filled."""
        dust = dict(self.QUOTE, ask_size=0.02)
        rows = {r["venue"]: r for r in execution.venues(dust, "home", 0.55)["rows"]}
        self.assertFalse(rows["kalshi_take"]["fillable"])
        self.assertNotEqual(execution.venues(dust, "home", 0.55)["best"], "kalshi_take")
        # Resting posts a new order rather than consuming one, so it survives.
        self.assertTrue(rows["kalshi_rest"]["fillable"])

    def test_two_venues_a_hair_apart_are_level_not_ranked(self):
        """A live game crowned a winner by 0.056c while the sportsbook price was
        only assumed -- and that assumption is worth ~1.2c between -105 and -110.
        Inside the tie band there is no winner to name."""
        # 52c resting breaks even at 52.44%, against the book's 52.38%.
        hair = {"bid": 0.47, "ask": 0.48, "bid_size": 900, "ask_size": 900,
                "home_strike": True}
        v = execution.venues(hair, "away", 0.55)
        self.assertTrue(v["tied"], "0.06c apart is not a ranking")
        self.assertIsNone(v["best"])
        self.assertFalse(any(r["best"] for r in v["rows"]))
        self.assertLess(v["margin"], config.EXEC_TIE_POINTS)

    def test_a_real_gap_still_names_a_winner(self):
        clear = {"bid": 0.40, "ask": 0.42, "bid_size": 900, "ask_size": 900,
                 "home_strike": True}
        v = execution.venues(clear, "home", 0.55)
        self.assertFalse(v["tied"])
        self.assertEqual(v["best"], "kalshi_rest")
        self.assertGreaterEqual(v["margin"], config.EXEC_TIE_POINTS)

    def test_no_rung_leaves_the_book_as_the_only_venue(self):
        """Ordinary, not broken: the ladder's grid is coarser than the Vegas
        number on most games -- 4 of 6 master picks on a measured slate."""
        v = execution.venues(None, "home", 0.55)
        self.assertEqual([r["venue"] for r in v["rows"]], ["book"])
        self.assertEqual(v["best"], "book")
        self.assertFalse(v["quoted"])
        self.assertIsNone(v["disagreement"])

    def test_the_sportsbook_price_changes_the_answer(self):
        """CFBD publishes the number but not the juice, so the price is assumed
        and overridable. It is not cosmetic: -105 moves the bar 1.2 points."""
        at110 = execution.venues(self.QUOTE, "home", 0.55, -110)
        at105 = execution.venues(self.QUOTE, "home", 0.55, -105)
        self.assertGreater(
            {r["venue"]: r for r in at110["rows"]}["book"]["break_even"],
            {r["venue"]: r for r in at105["rows"]}["book"]["break_even"])

    def test_a_payload_without_quotes_is_not_a_missing_rung(self):
        """Two different empty states, and conflating them lies. A payload built
        before quotes existed has no `quotes` key at all -- every box would claim
        "Kalshi does not quote this number" when Kalshi quotes plenty of them.
        The page tells them apart on the key; both reach venues() the same way."""
        self.assertIsNone(execution.quote_at({}, 3.5), "no quotes key at all")
        self.assertIsNone(execution.quote_at({"quotes": []}, 3.5), "ladder unreadable")
        self.assertIsNone(
            execution.quote_at({"quotes": [[7.5, 0.5, 0.52, 10, 10]]}, 3.5),
            "quoted, but not at this number")
        for game in ({}, {"quotes": []}, {"quotes": [[7.5, 0.5, 0.52, 10, 10]]}):
            v = execution.venues(execution.quote_at(game, 3.5), "home", 0.55)
            self.assertFalse(v["quoted"])
            self.assertEqual([r["venue"] for r in v["rows"]], ["book"])

    def test_quote_at_reads_the_side_off_the_sign(self):
        game = {"quotes": [[-7.5, 0.30, 0.32, 10, 10], [3.5, 0.60, 0.62, 10, 10]]}
        self.assertTrue(execution.quote_at(game, 3.5)["home_strike"])
        self.assertFalse(execution.quote_at(game, -7.5)["home_strike"])
        self.assertIsNone(execution.quote_at(game, 1.5), "no rung at that number")
        self.assertIsNone(execution.quote_at(game, None))


class TestExecutionQuotesPublished(unittest.TestCase):
    def test_sizes_survive_parsing(self):
        from pipeline.margin_model import parse_ladder
        ladder = parse_ladder(logistic_event())
        self.assertTrue(all(s.bid_size > 0 and s.ask_size > 0 for s in ladder.strikes))
        self.assertTrue(ladder.strikes[0].executable("ask"))

    def test_a_dust_quote_is_not_executable(self):
        event = logistic_event()
        event["markets"][0]["yes_ask_size_fp"] = "0.02"
        from pipeline.margin_model import parse_ladder
        strike = parse_ladder(event).strikes[0]
        self.assertFalse(strike.executable("ask"))
        self.assertTrue(strike.executable("bid"),
                        "one thin side must not condemn the other")

    def test_quotes_stay_near_the_number(self):
        """Range-limited on purpose: the whole ladder for a slate is ~29KB of
        payload, and MAX_STRIKE_DISTANCE already refuses a line more than 3pts
        from a strike."""
        from pipeline import build_predictions
        from pipeline.margin_model import parse_ladder, read_market
        ladder = parse_ladder(logistic_event())
        read = read_market(ladder)
        rows = build_predictions._quotes(ladder, read)
        self.assertTrue(rows)
        for x, bid, ask, bid_size, ask_size in rows:
            self.assertLessEqual(abs(x - read.implied_margin),
                                 config.EXEC_QUOTE_RANGE_PTS)
            self.assertLessEqual(bid, ask)
        self.assertEqual(rows, sorted(rows), "the page scans these in order")

    def test_big_sizes_are_published_whole_and_dust_is_not(self):
        """Two decimals on 9776.33 is payload nothing reads; 0.02 is the
        evidence that a quote is seeded dust rather than a market."""
        from pipeline import build_predictions
        self.assertEqual(build_predictions._size(9776.33), 9776)
        self.assertEqual(build_predictions._size(0.02), 0.02)


if __name__ == "__main__":
    unittest.main()
