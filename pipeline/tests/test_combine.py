import unittest

from pipeline import combine, config, sp_plus


def sig(key, margin, sigma):
    return combine.Signal(key, key, margin, sigma)


class TestPrecisionWeighting(unittest.TestCase):
    def test_sharper_signal_dominates(self):
        """Reproduces the measured Arizona St./Texas A&M case: the ladder pins
        the spread far tighter than the moneyline, so it carries the blend."""
        c = combine.combine([sig("kalshi_spread", 14.14, 0.150),
                             sig("kalshi_ml", 15.37, 0.415)])
        self.assertAlmostEqual(c.shares["kalshi_spread"], 0.884, delta=0.02)
        self.assertAlmostEqual(c.margin, 14.28, delta=0.05)

    def test_equal_precision_is_a_straight_average(self):
        c = combine.combine([sig("a", 10.0, 0.5), sig("b", 14.0, 0.5)])
        self.assertAlmostEqual(c.margin, 12.0, places=6)
        self.assertAlmostEqual(c.shares["a"], 0.5, places=6)

    def test_combined_sigma_never_beats_the_sharper_input(self):
        """Inverse-variance weighting assumes independent errors. These are the
        same exchange and largely the same traders, so the textbook formula
        would report the blend as sharper than either input."""
        c = combine.combine([sig("a", 10.0, 0.20), sig("b", 12.0, 0.60)])
        self.assertAlmostEqual(c.sigma, 0.20, places=6)
        naive = (1 / (1 / 0.20 ** 2 + 1 / 0.60 ** 2)) ** 0.5
        self.assertGreater(c.sigma, naive)

    def test_displayed_shares_always_sum_to_100(self):
        """Rounding each share independently can total 99 or 101, which reads as
        a bug on screen. Emitting whole percents once also stops the page and the
        stored reason from disagreeing by a point."""
        for a, b in ((0.150, 0.415), (0.41, 0.25), (0.14, 0.21), (0.5, 0.5), (0.1, 0.9)):
            c = combine.combine([sig("kalshi_spread", 14.0, a), sig("kalshi_ml", 15.0, b)])
            pct = c.percent_shares()
            self.assertEqual(sum(pct.values()), 100, f"sigma {a}/{b} -> {pct}")
            self.assertIn(f"{pct['kalshi_spread']}% kalshi spread", c.describe())

    def test_unavailable_signals_are_skipped(self):
        c = combine.combine([sig("a", 10.0, 0.3), sig("b", None, None)])
        self.assertEqual(c.used, ["a"])
        self.assertAlmostEqual(c.margin, 10.0)

    def test_no_signals_at_all(self):
        c = combine.combine([sig("a", None, None)])
        self.assertIsNone(c.margin)
        self.assertEqual(c.describe(), "no signal")

    def test_sp_plus_is_not_in_the_master(self):
        """SP+ grades near break-even against the spread, so it is a lens only."""
        ladder = sig("kalshi_spread", 14.0, 0.15)
        ml = sig("kalshi_ml", 15.0, 0.40)
        master = combine.master_composite(ladder, ml)
        self.assertNotIn("sp_plus", master.shares)
        self.assertEqual(sorted(master.used), ["kalshi_ml", "kalshi_spread"])


class TestSpPlus(unittest.TestCase):
    def setUp(self):
        self.idx = sp_plus.index_ratings({"Ohio State": 25.3, "Texas": 18.1})

    def test_rating_gap_plus_home_field(self):
        p = sp_plus.project(self.idx, "Texas", "Ohio St.")
        self.assertAlmostEqual(p.home_favored_by,
                               (18.1 - 25.3) + config.HOME_FIELD_ADVANTAGE, places=6)

    def test_neutral_site_drops_home_field(self):
        p = sp_plus.project(self.idx, "Texas", "Ohio St.", neutral_site=True)
        self.assertAlmostEqual(p.home_favored_by, 18.1 - 25.3, places=6)
        self.assertEqual(p.home_field, 0.0)

    def test_kalshi_spelling_matches_cfbd(self):
        """Kalshi writes 'Ohio St.', CFBD writes 'Ohio State'."""
        self.assertIsNotNone(sp_plus.project(self.idx, "Texas", "Ohio St."))

    def test_unrated_opponent_returns_nothing(self):
        """FCS teams mostly have no SP+ rating; inventing one would be worse
        than showing nothing."""
        self.assertIsNone(sp_plus.project(self.idx, "Texas", "Some FCS School"))


if __name__ == "__main__":
    unittest.main()
