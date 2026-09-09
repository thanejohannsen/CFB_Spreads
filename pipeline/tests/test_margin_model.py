import math
import unittest

from pipeline import config, liquidity, moneyline, predict
from pipeline.margin_model import (
    cover_probabilities, pava_non_increasing, parse_ladder, read_market,
)
from pipeline.tests.factories import logistic_event, market


class TestParsing(unittest.TestCase):
    def test_orientation_from_title(self):
        ladder = parse_ladder(logistic_event())
        self.assertEqual(ladder.home_team, "Boston College")
        self.assertEqual(ladder.away_team, "Rutgers")
        self.assertFalse(ladder.one_sided)

    def test_one_sided_ladder_is_readable(self):
        """Blowouts are quoted on the favourite's side only; that is still a
        usable constraint, not a parse failure."""
        event = logistic_event(home="Miami (FL)", away="Florida A&M",
                               home_abbrev="MIA", away_abbrev="FAMU",
                               true_margin=40.0, scale=12.0,
                               thresholds=[30.5, 34.5, 38.5, 42.5, 46.5,
                                           50.5, 54.5, 58.5, 62.5])
        event["markets"] = [m for m in event["markets"] if "-MIA" in m["ticker"]]
        ladder = parse_ladder(event)
        self.assertIsNotNone(ladder)
        self.assertTrue(ladder.one_sided)
        self.assertEqual(ladder.home_team, "Miami (FL)")
        self.assertEqual(ladder.away_team, "Florida A&M")
        self.assertAlmostEqual(read_market(ladder).implied_margin, 40.0, delta=1.5)


class TestGameDate(unittest.TestCase):
    def test_date_comes_from_the_ticker_not_the_close_time(self):
        """close_time is a settlement deadline that can fall days after the
        game, so it must never be mistaken for the kickoff."""
        event = logistic_event()
        event["event_ticker"] = "KXNCAAFSPREAD-26SEP12OKLAMICH"
        for m in event["markets"]:
            m["close_time"] = "2026-09-14T23:00:00Z"      # the Monday after
        ladder = parse_ladder(event)
        self.assertEqual(ladder.game_date, "2026-09-12")

    def test_unparseable_ticker_yields_no_date(self):
        event = logistic_event()
        event["event_ticker"] = "KXNCAAFSPREAD-GARBAGE"
        self.assertIsNone(parse_ladder(event).game_date)


class TestIsotonic(unittest.TestCase):
    def test_pava_enforces_non_increasing(self):
        pts = [(-2.0, 0.60), (-1.0, 0.72), (0.0, 0.50), (1.0, 0.55), (2.0, 0.30)]
        out = pava_non_increasing([(x, y, 1.0) for x, y in pts])
        ys = [y for _, y in out]
        self.assertEqual(ys, sorted(ys, reverse=True))

    def test_pava_preserves_already_valid_input(self):
        pts = [(0.0, 0.9), (1.0, 0.6), (2.0, 0.3)]
        out = pava_non_increasing([(x, y, 1.0) for x, y in pts])
        for (_, got), (_, want) in zip(out, pts):
            self.assertAlmostEqual(got, want, places=9)


class TestSurvivalCurve(unittest.TestCase):
    def setUp(self):
        self.read = read_market(parse_ladder(logistic_event(true_margin=3.0, scale=9.0)))

    def test_recovers_known_median(self):
        self.assertAlmostEqual(self.read.implied_margin, 3.0, delta=0.4)

    def test_table_is_monotone_non_increasing(self):
        table = self.read.table()
        self.assertEqual(len(table), config.MARGIN_MAX - config.MARGIN_MIN)
        for a, b in zip(table, table[1:]):
            self.assertGreaterEqual(a, b)

    def test_tails_stay_in_range(self):
        self.assertGreater(self.read.curve_mid.at(-200.0), 0.99)
        self.assertLess(self.read.curve_mid.at(200.0), 0.01)

    def test_band_matches_analytic_prediction(self):
        """The band is not a free parameter: for a logistic margin distribution
        the survival slope at the median is 1/(4*scale), so a bid/ask width w
        must translate into a band of about 4*scale*w points.  Checking that
        relationship pins the whole envelope construction, not just its size."""
        for scale, width in ((9.0, 0.02), (12.0, 0.02), (9.0, 0.04)):
            read = read_market(parse_ladder(
                logistic_event(true_margin=3.0, scale=scale, width=width)))
            self.assertAlmostEqual(read.band, 4 * scale * width,
                                   delta=0.25,
                                   msg=f"scale={scale} width={width}")

    def test_narrower_quotes_give_a_narrower_band(self):
        wide = read_market(parse_ladder(logistic_event(width=0.06))).band
        tight = read_market(parse_ladder(logistic_event(width=0.01))).band
        self.assertLess(tight, wide)


class TestCoverProbabilities(unittest.TestCase):
    def setUp(self):
        self.curve = read_market(parse_ladder(logistic_event(true_margin=3.0))).curve_mid

    def test_half_point_line_has_no_push(self):
        p = cover_probabilities(self.curve, 7.5)
        self.assertEqual(p.push, 0.0)
        self.assertAlmostEqual(p.home + p.away, 1.0, places=6)

    def test_whole_number_line_has_push(self):
        p = cover_probabilities(self.curve, 7.0)
        self.assertGreater(p.push, 0.0)
        self.assertAlmostEqual(p.home + p.away + p.push, 1.0, places=6)

    def test_discrete_margins_treat_n_and_n_plus_half_alike(self):
        """P(margin > 7) and P(margin > 7.5) are the same event for integer
        margins, so covering -7 outright must match covering -7.5."""
        whole = cover_probabilities(self.curve, 7.0)
        half = cover_probabilities(self.curve, 7.5)
        self.assertAlmostEqual(whole.home, half.home, places=6)

    def test_line_at_median_is_a_coin_flip(self):
        p = cover_probabilities(self.curve, 3.0)
        self.assertAlmostEqual(p.home, p.away, delta=0.08)


class TestStrikeFiltering(unittest.TestCase):
    def test_rejects_the_real_world_wide_quote(self):
        """A market quoted 0.08 / 0.88 was observed live; its midpoint is
        meaningless and must never reach the model."""
        event = logistic_event()
        event["markets"].append(
            market(event["event_ticker"], "BC", "Boston College", 25.5, 0.08, 0.88, 0.0, 0.0)
        )
        ladder = parse_ladder(event)
        usable = {s.ticker for s in ladder.usable_strikes()}
        self.assertNotIn(f"{event['event_ticker']}-BC26", usable)

    def test_too_few_usable_strikes_is_refused(self):
        event = logistic_event(thresholds=[3.5, 7.5])
        read = read_market(parse_ladder(event))
        self.assertIsNotNone(read.rejected)
        self.assertEqual(liquidity.grade(read).tier, "D")


class TestTiering(unittest.TestCase):
    def _grade(self, **kwargs):
        return liquidity.grade(read_market(parse_ladder(logistic_event(**kwargs))))

    def test_liquid_tight_ladder_is_tier_a(self):
        self.assertEqual(self._grade(width=0.01, oi=20000).tier, "A")

    def test_thin_ladder_falls_below_tier_a(self):
        self.assertIn(self._grade(width=0.06, oi=600, traded=False).tier, ("B", "C", "D"))

    def test_zero_open_interest_is_tier_d(self):
        self.assertEqual(self._grade(width=0.01, oi=0.0, traded=False).tier, "D")


class TestShrinkage(unittest.TestCase):
    def test_tight_ladder_barely_moves_toward_prior(self):
        read = read_market(parse_ladder(logistic_event(width=0.01, oi=20000)))
        blend = liquidity.shrink(read, prior_margin=10.0)
        # band ~0.36pt against a 1.5pt prior sigma: precision weighting puts
        # roughly 95% on Kalshi, so the prior barely registers.
        self.assertGreater(blend.kalshi_weight, 0.90)
        self.assertAlmostEqual(blend.margin, read.implied_margin, delta=0.5)

    def test_no_prior_leaves_estimate_untouched(self):
        read = read_market(parse_ladder(logistic_event()))
        blend = liquidity.shrink(read, prior_margin=None)
        self.assertEqual(blend.kalshi_weight, 1.0)
        self.assertEqual(blend.margin, read.implied_margin)
        self.assertEqual(blend.describe(), "100% Kalshi")

    def test_wider_band_pulls_further_toward_prior(self):
        tight = liquidity.shrink(read_market(parse_ladder(logistic_event(width=0.01))), 20.0)
        loose = liquidity.shrink(read_market(parse_ladder(logistic_event(width=0.14))), 20.0)
        self.assertLess(loose.kalshi_weight, tight.kalshi_weight)


class TestPickRules(unittest.TestCase):
    def _pick(self, line, **kwargs):
        read = read_market(parse_ladder(logistic_event(**kwargs)))
        quality = liquidity.grade(read)
        blend = liquidity.shrink(read, line)
        return predict.evaluate(read, quality, blend, line, "Boston College", "Rutgers")

    def test_line_inside_band_is_no_play(self):
        read = read_market(parse_ladder(logistic_event(true_margin=3.0, width=0.01)))
        pick = self._pick(round(read.implied_margin, 2), width=0.01)
        self.assertIsNone(pick.side)
        self.assertEqual(pick.confidence, "no-play")
        self.assertIn("uncertainty band", pick.reason)

    def test_unreadable_ladder_never_produces_a_pick(self):
        pick = self._pick(7.5, thresholds=[3.5, 7.5])
        self.assertIsNone(pick.side)
        self.assertEqual(pick.confidence, "no-signal")
        self.assertIn("Insufficient market", pick.reason)

    def test_tiny_edge_outside_the_band_is_still_no_play(self):
        """The band and the vig floor catch different failures. A tight ladder
        can clear the band with a 0.2pt edge that is economically meaningless;
        measured live, the band test alone passed 16 such picks in one week."""
        read = read_market(parse_ladder(logistic_event(true_margin=3.0, width=0.01, oi=20000)))
        self.assertLess(read.band, 0.5)                 # band would not catch it
        line = round(read.implied_margin - 0.4, 2)
        self.assertFalse(read.margin_low <= line <= read.margin_high)
        pick = self._pick(line, width=0.01, oi=20000)
        self.assertIsNone(pick.side)
        self.assertEqual(pick.confidence, "no-play")
        self.assertIn("inside the vig", pick.reason)

    def test_edge_just_over_the_floor_does_produce_a_pick(self):
        read = read_market(parse_ladder(logistic_event(true_margin=3.0, width=0.01, oi=20000)))
        pick = self._pick(round(read.implied_margin - 1.6, 2), width=0.01, oi=20000)
        self.assertEqual(pick.side, "home")

    def test_clear_disagreement_produces_a_side(self):
        pick = self._pick(-6.5, true_margin=3.0, width=0.01, oi=20000)
        self.assertEqual(pick.side, "home")
        self.assertEqual(pick.team, "Boston College")
        self.assertGreater(pick.edge, 0)

    def test_missing_line_is_not_a_pick(self):
        read = read_market(parse_ladder(logistic_event()))
        pick = predict.evaluate(read, liquidity.grade(read),
                                liquidity.shrink(read, None), None, "BC", "RUTG")
        self.assertIsNone(pick.side)


class TestMoneylineCrossCheck(unittest.TestCase):
    def setUp(self):
        self.read = read_market(parse_ladder(logistic_event(true_margin=3.0, scale=9.0,
                                                            width=0.01, oi=20000)))

    def _quote(self, p, band=0.01):
        class Q:
            home_prob = p
            away_prob = 1 - p
            home_prob_low = p - band / 2
            home_prob_high = p + band / 2
            open_interest = 500_000.0
        Q.band = band
        return Q()

    def test_agreement_round_trips_to_the_same_margin(self):
        """Feeding back the ladder's own win probability must reproduce its
        median exactly -- that pins the probability/margin conversion."""
        implied_p = self.read.curve_mid.at(0.0)
        check = moneyline.cross_check(self.read, self._quote(implied_p))
        self.assertAlmostEqual(check.ml_margin, self.read.implied_margin, delta=0.05)
        self.assertFalse(check.significant)

    def test_real_disagreement_is_flagged(self):
        implied_p = self.read.curve_mid.at(0.0)
        check = moneyline.cross_check(self.read, self._quote(implied_p + 0.10))
        self.assertTrue(check.significant)
        self.assertGreater(abs(check.divergence_pts), 0)

    def test_tail_conversion_is_not_called_significant(self):
        lo, hi = self.read.strike_span()
        check = moneyline.cross_check(self.read, self._quote(0.95))
        self.assertFalse(check.significant)
        self.assertGreater(check.ml_margin, hi)
        self.assertIn("extrapolation", check.note)

    def test_near_certainty_is_suppressed(self):
        check = moneyline.cross_check(self.read, self._quote(0.995))
        self.assertIsNone(check.ml_margin)
        self.assertFalse(check.significant)

    def test_american_odds_conversion(self):
        self.assertEqual(moneyline.american_odds(0.50), -100)
        self.assertEqual(moneyline.american_odds(0.80), -400)
        self.assertEqual(moneyline.american_odds(0.20), 400)
        self.assertIsNone(moneyline.american_odds(0.0))


if __name__ == "__main__":
    unittest.main()
