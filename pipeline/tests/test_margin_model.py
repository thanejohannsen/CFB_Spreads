import math
import unittest

from pipeline import combine, config, liquidity, moneyline, predict
from pipeline.margin_model import (
    cover_probabilities, margin_quantile, margin_survival, pava_non_increasing,
    parse_ladder, read_market,
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

    def test_fitted_curve_tracks_the_strikes_it_came_from(self):
        """The residual is the honesty check on a two-parameter summary. These
        prices ARE logistic, so the fit must be near-exact; a real market that
        is genuinely lopsided is what the number exists to flag."""
        self.assertLess(self.read.scale_residual, 0.02)

    def test_residual_separates_shape_from_quote_noise(self):
        """Calibration for SCALE_RESIDUAL_FLAG. Prices from an exact logistic,
        degraded only by Kalshi's 1c ticks and a realistic spread, must come
        back near zero -- otherwise the residual is measuring the machinery and
        a flag built on it would fire on nothing meaningful. Real ladders sit
        about six times higher, which is how we know their shape is genuinely
        not logistic."""
        from pipeline.margin_model import margin_survival
        from pipeline.tests.factories import market
        ev = "KXNCAAFSPREAD-26SEP19SYNTH"
        markets = []
        for t in [1.5 + 2 * i for i in range(24)]:
            p = 1.0 - margin_survival(-7.5, 8.8, -t)
            p = min(0.97, max(0.03, p))
            mid = round(p / 0.01) * 0.01                  # 1c ticks
            markets.append(market(ev, "AWY", "Away", t, mid - 0.0075, mid + 0.0075, 20000.0))
        read = read_market(parse_ladder(
            {"event_ticker": ev, "title": "Away vs Home: Spread", "markets": markets}))
        self.assertIsNone(read.rejected)
        self.assertLess(read.scale_residual, config.SCALE_RESIDUAL_FLAG / 4,
                        "an exactly logistic market must not look like a misfit")

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
        self.read = read_market(parse_ladder(logistic_event(true_margin=3.0)))
        self.center = self.read.implied_margin
        self.scale = self.read.scale

    def _p(self, line):
        return cover_probabilities(self.center, self.scale, line)

    def test_half_point_line_has_no_push(self):
        p = self._p(7.5)
        self.assertEqual(p.push, 0.0)
        self.assertAlmostEqual(p.home + p.away, 1.0, places=6)

    def test_whole_number_line_has_push(self):
        p = self._p(7.0)
        self.assertGreater(p.push, 0.0)
        self.assertAlmostEqual(p.home + p.away + p.push, 1.0, places=6)

    def test_discrete_margins_treat_n_and_n_plus_half_alike(self):
        """P(margin > 7) and P(margin > 7.5) are the same event for integer
        margins, so covering -7 outright must match covering -7.5."""
        self.assertAlmostEqual(self._p(7.0).home, self._p(7.5).home, places=6)

    def test_line_at_median_is_a_coin_flip(self):
        p = self._p(3.0)
        self.assertAlmostEqual(p.home, p.away, delta=0.08)

    def test_scale_sets_the_points_to_probability_exchange_rate(self):
        """The bug this guards: probability per point used to come from the
        local slope between two adjacent strikes, which is 1c-tick noise. Across
        one slate that ran 1.5% to 10.0% per point, the steepest implying a
        4-point margin SD. A fitted scale has to land near reality instead."""
        per_point = self._p(self.center - 0.5).home - self._p(self.center + 0.5).home
        self.assertGreater(per_point, 0.020, "a CFB point is worth more than 2%")
        self.assertLess(per_point, 0.040, "and much less than the 10% the local slope claimed")
        # 1/(4*scale) is the logistic density at its own median.
        self.assertAlmostEqual(per_point, 1.0 / (4.0 * self.scale), places=3)

    def test_quantile_inverts_the_survival_function(self):
        for p in (0.10, 0.25, 0.50, 0.75, 0.90):
            x = margin_quantile(self.center, self.scale, p)
            self.assertAlmostEqual(margin_survival(self.center, self.scale, x), p, places=9)

    def test_quantile_has_no_floor_to_run_out_of(self):
        """The strike curve goes flat past its last strike, so a moneyline
        priced beyond it had no spread equivalent at all. A logistic always
        converts; whether to TRUST it is the strike-span check's job."""
        self.assertIsNotNone(margin_quantile(self.center, self.scale, 0.001))
        self.assertIsNone(margin_quantile(self.center, self.scale, 0.0))


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

    def test_s_tier_needs_volume_and_quality(self):
        """S is A plus serious money. Requiring A's bars as well stops a heavily
        traded game with a loose band outranking a tight one."""
        tight = read_market(parse_ladder(logistic_event(width=0.01, oi=20000)))
        self.assertEqual(liquidity.grade(tight, 0).tier, "A")
        self.assertEqual(liquidity.grade(tight, config.S_TIER_DOLLAR_VOLUME).tier, "S")
        self.assertEqual(liquidity.grade(tight, config.S_TIER_DOLLAR_VOLUME - 1).tier, "A")

        loose = read_market(parse_ladder(logistic_event(width=0.06, oi=20000)))
        self.assertNotEqual(liquidity.grade(loose, 5_000_000).tier, "S",
                            "volume alone must not promote a loose band")

    def test_dollar_volume_is_contracts_times_price(self):
        """Kalshi settles each contract $0-$1, so a contract traded at 50c moved
        50c. Counting notional would roughly double the figure."""
        ladder = parse_ladder(logistic_event(width=0.01))
        for s in ladder.strikes:
            s.volume = 1000.0
        expected = sum(1000.0 * s.mid for s in ladder.strikes)
        self.assertAlmostEqual(ladder.dollar_volume, expected, places=6)
        self.assertLess(ladder.dollar_volume, 1000.0 * len(ladder.strikes),
                        "notional would be strictly larger")

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
    def _pick(self, line, mode="master", **kwargs):
        read = read_market(parse_ladder(logistic_event(**kwargs)))
        quality = liquidity.grade(read)
        blend = liquidity.shrink(read, line)
        band = None if read.rejected else (read.margin_low, read.margin_high)
        return predict.evaluate(blend.margin if not read.rejected else None, line,
                                "Boston College", "Rutgers", mode=mode, read=read,
                                quality=quality, band=band,
                                unavailable=read.rejected)

    def test_line_inside_band_is_no_play(self):
        read = read_market(parse_ladder(logistic_event(true_margin=3.0, width=0.01)))
        pick = self._pick(round(read.implied_margin, 2), width=0.01)
        self.assertIsNone(pick.side)
        self.assertEqual(pick.confidence, "no-play")
        self.assertIn("uncertainty band", pick.reason)

    def test_line_exactly_on_the_published_band_edge_is_no_play(self):
        """The bug this pins, with the numbers it was found on.

        Houston vs Texas Tech published a band of [7.12, 7.50] against a 7.5
        line. The pipeline tested the line against the *unrounded* margin_high
        (a hair under 7.5), fell through, and stored "lean, take Houston"; the
        page could only read 7.50, found the line inside the band, and rendered
        No play. The board told you to pass on a game the record counted.

        Both sides must decide from the published number, and the band test is
        inclusive, so a line sitting exactly on either edge is a no-play. The
        cross-implementation check lives in tools/conformance.js, which replays
        docs/app.js itself rather than adding a third copy of these rules."""
        for line in (7.12, 7.5):
            pick = predict.evaluate(8.61, line, "Houston", "Texas Tech",
                                    mode="master", band=(7.12, 7.5))
            self.assertIsNone(pick.side, f"line {line} sits on the band edge")
            self.assertEqual(pick.confidence, "no-play")
            self.assertIn("uncertainty band", pick.reason)

    def test_just_outside_the_published_band_can_still_pick(self):
        """The edge case only refuses *on* the boundary; a hair outside is live,
        which is what makes deciding at the published precision matter."""
        pick = predict.evaluate(8.61, 7.51, "Houston", "Texas Tech",
                                mode="master", band=(7.12, 7.5))
        self.assertEqual(pick.side, "home")

    def test_edge_exactly_at_the_vig_floor_is_a_pick(self):
        """`< MIN_EDGE_POINTS`, not `<=`. Pinned because app.js repeats the
        comparison and an off-by-one-strictness there is invisible until a
        number lands exactly on it."""
        at_floor = predict.evaluate(10.0, 9.0, "Home", "Away", mode="lens")
        self.assertEqual(at_floor.side, "home")
        self.assertAlmostEqual(abs(at_floor.edge), config.MIN_EDGE_POINTS)

        under = predict.evaluate(9.99, 9.0, "Home", "Away", mode="lens")
        self.assertIsNone(under.side)
        self.assertIn("inside the vig", under.reason)

    def test_confidence_thresholds_are_exact_at_the_boundary(self):
        """An edge of exactly EDGE_SOLID is solid, not lean; exactly
        EDGE_STRONG is strong. Same strictness as app.js."""
        solid = predict.evaluate(9.0 + config.EDGE_SOLID, 9.0, "Home", "Away", mode="lens")
        self.assertEqual(solid.confidence, "solid")
        strong = predict.evaluate(9.0 + config.EDGE_STRONG, 9.0, "Home", "Away", mode="lens")
        self.assertEqual(strong.confidence, "strong")

    def test_publish_quantises_to_the_precision_the_page_reads(self):
        """config.publish is what keeps the two implementations in step: the
        pipeline must decide from the number it writes, not a sharper one."""
        self.assertEqual(config.publish(7.4999999), 7.5)
        self.assertEqual(config.publish(0.98765, config.PUBLISH_WEIGHT_DP), 0.9877)
        self.assertIsNone(config.publish(None))

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
        pick = predict.evaluate(read.implied_margin, None, "BC", "RUTG",
                                read=read, quality=liquidity.grade(read))
        self.assertIsNone(pick.side)

    def test_lens_mode_skips_the_band_test(self):
        """A lens exists to measure whether its signal carries information, so
        it must not be filtered through the master's safety rules."""
        read = read_market(parse_ladder(logistic_event(true_margin=3.0, width=0.01, oi=20000)))
        line = round(read.implied_margin - 1.4, 2)
        master = self._pick(line, width=0.01, oi=20000)
        lens = predict.evaluate(read.implied_margin, line, "Boston College", "Rutgers",
                                mode="lens", read=read)
        self.assertEqual(lens.side, "home")
        self.assertIsNotNone(lens.estimate)
        # master shrinks toward the line, so its edge is strictly smaller
        self.assertLess(abs(master.edge), abs(lens.edge))

    def test_lens_still_honours_the_vig_floor(self):
        read = read_market(parse_ladder(logistic_event(true_margin=3.0, width=0.01)))
        pick = predict.evaluate(read.implied_margin, round(read.implied_margin - 0.3, 2),
                                "BC", "RUTG", mode="lens", read=read)
        self.assertIsNone(pick.side)
        self.assertIn("inside the vig", pick.reason)

    def test_lens_works_without_a_ladder(self):
        """SP+ can still pick on a game whose Kalshi market is unreadable; the
        cover probability is left blank rather than invented."""
        pick = predict.evaluate(9.0, 3.5, "BC", "RUTG", mode="lens", read=None)
        self.assertEqual(pick.side, "home")
        self.assertIsNone(pick.p_cover)


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

    def test_moneyline_below_the_ladder_floor_explains_itself(self):
        """A heavy favourite can price its last two strikes at the same cents.
        Two knots at one probability give no slope, so no upper tail is fitted
        and the curve goes flat -- the ladder then cannot express any
        probability below that value, and the conversion has nowhere to land.

        Refusing is right; reporting it as a missing market is not. This is the
        Georgia/Arkansas case: Kalshi quoted a moneyline with 48k open interest
        while the page said "no comparable moneyline"."""
        event = "KXNCAAFSPREAD-26SEP19UGAARK"
        prices = {1.5: 0.925, 2.5: 0.925, 3.5: 0.915, 5.5: 0.895, 7.5: 0.87,
                  10.5: 0.83, 14.5: 0.77, 17.5: 0.72, 21.5: 0.64, 28.5: 0.50,
                  41.5: 0.28}
        read = read_market(parse_ladder({
            "event_ticker": event,
            "title": "Georgia vs Arkansas: Spread",
            "markets": [market(event, "UGA", "Georgia", t, p - 0.01, p + 0.01, 20000.0)
                        for t, p in prices.items()],
        }))
        self.assertIsNone(read.rejected)
        floor = read.curve_mid.at(read.curve_mid.xs[-1] + 60.0)
        self.assertAlmostEqual(floor, 0.075, places=3)

        check = moneyline.cross_check(read, self._quote(0.045))
        self.assertIsNotNone(check, "a quote exists; this is not a missing market")

        # Fitting a distribution removes the floor entirely, so the conversion
        # now succeeds -- and lands inside the range the market actually quoted,
        # which is what makes it trustworthy rather than a tail read.
        self.assertIsNotNone(check.ml_margin)
        lo, hi = read.strike_span()
        self.assertTrue(lo - 1.0 <= check.ml_margin <= hi + 1.0,
                        f"{check.ml_margin} should sit inside the traded strikes {lo}..{hi}")
        signal = combine.moneyline_signal(check)
        self.assertIsNotNone(signal.margin, "a 48k-open-interest market must not be discarded")

    def test_conversion_beyond_the_traded_strikes_is_not_called_significant(self):
        """Removing the floor must not mean trusting the tail. A conversion
        landing outside the strikes is still refused a significance flag."""
        read = read_market(parse_ladder(logistic_event(true_margin=3.0, width=0.01, oi=20000)))
        _lo, hi = read.strike_span()
        check = moneyline.cross_check(read, self._quote(0.95))
        self.assertGreater(check.ml_margin, hi)
        self.assertFalse(check.significant)
        self.assertIn("extrapolation", check.note)

    def test_absent_market_still_reads_as_absent(self):
        """The generic note keeps its meaning: it fires only with no quote."""
        self.assertIsNone(moneyline.cross_check(self.read, None))
        self.assertEqual(combine.moneyline_signal(None).note, "no comparable moneyline")

    def test_american_odds_conversion(self):
        self.assertEqual(moneyline.american_odds(0.50), -100)
        self.assertEqual(moneyline.american_odds(0.80), -400)
        self.assertEqual(moneyline.american_odds(0.20), 400)
        self.assertIsNone(moneyline.american_odds(0.0))


if __name__ == "__main__":
    unittest.main()
