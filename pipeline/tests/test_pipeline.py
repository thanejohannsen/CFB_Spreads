import datetime
import json
import os
import tempfile
import unittest

from pipeline import grade_history, select_slate, weeks
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
    """The bug: the slate is the top N by open interest, recomputed every run,
    but a lock is permanent. A game locked on Wednesday could be pushed out of
    the top N by Saturday -- after which the board stopped showing it while the
    record went on grading it as pending, and its T-1h lock could never be
    refreshed again, because record() only ever sees games in the payload.

    Observed on Michigan St. vs Notre Dame: locked as a master lean, then 32nd
    by open interest, so the board read "nothing clears the threshold" while the
    record carried a pending pick on it."""

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

    def test_before_its_final_lock_even_a_no_play_is_pinned(self):
        """Its T-1h snapshot has not been taken yet, and a game absent from the
        payload is a game record() cannot refresh."""
        self._lock("KX-NOPLAY", side=None)
        self.assertIn("KX-NOPLAY",
                      grade_history.locked_ids("2026-09-12", self.dir, now=self.early))


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


if __name__ == "__main__":
    unittest.main()
