"""Builders for synthetic Kalshi payloads used by the tests."""

from __future__ import annotations

import math


def market(event: str, abbrev: str, team: str, threshold: float,
           bid: float, ask: float, oi: float = 5000.0, last: float = None) -> dict:
    tag = int(threshold + 0.5)
    return {
        "ticker": f"{event}-{abbrev}{tag}",
        "event_ticker": event,
        "floor_strike": threshold,
        "yes_bid_dollars": f"{bid:.4f}",
        "yes_ask_dollars": f"{ask:.4f}",
        "open_interest_fp": f"{oi:.2f}",
        "last_price_dollars": f"{(bid + ask) / 2 if last is None else last:.4f}",
        "yes_sub_title": f"{team} wins by over {threshold} points",
        "close_time": "2026-09-12T23:00:00Z",
    }


def logistic_event(home="Boston College", away="Rutgers", home_abbrev="BC",
                   away_abbrev="RUTG", true_margin=3.0, scale=9.0,
                   width=0.02, oi=5000.0, thresholds=None,
                   event="KXNCAAFSPREAD-26SEP11RUTGBC", traded=True) -> dict:
    """An event whose prices come from a known logistic margin distribution.

    S(x) = P(margin > x) = 1 / (1 + exp((x - true_margin)/scale)), so the true
    median is `true_margin` and the model should recover it.
    """
    thresholds = thresholds or [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 9.5,
                                10.5, 11.5, 13.5, 14.5, 16.5, 17.5, 20.5]
    markets = []
    for t in thresholds:
        p_home = 1.0 / (1.0 + math.exp((t - true_margin) / scale))
        markets.append(market(event, home_abbrev, home, t,
                              max(0.01, p_home - width / 2), min(0.99, p_home + width / 2),
                              oi, None if traded else 0.0))
        # P(away wins by > t) = P(margin < -t) = 1 - S(-t)
        p_away = 1.0 - 1.0 / (1.0 + math.exp((-t - true_margin) / scale))
        markets.append(market(event, away_abbrev, away, t,
                              max(0.01, p_away - width / 2), min(0.99, p_away + width / 2),
                              oi, None if traded else 0.0))
    return {
        "event_ticker": event,
        "title": f"{away} vs {home}: Spread",
        "markets": markets,
    }
