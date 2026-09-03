"""Cumulative factor tests: ordering, right-edge identity, and as-of replay."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from nse_eod.transform.cumulative import (
    build_tr_factors,
    cumulative_factor_frame,
    cumulative_factors,
)


def D(x):
    return Decimal(str(x))


def date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


DATES = [date(d) for d in ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")]


class TestCumulativeFactors:
    def test_no_events_leaves_everything_at_one(self):
        cf = cumulative_factors([], DATES)
        assert set(cf.values()) == {D(1)}

    def test_event_applies_only_strictly_before_ex_date(self):
        """A 1:1 bonus with ex_date 2024-01-03 halves 01-01 and 01-02 only.

        The ex-date bar itself already trades at the post-bonus price, so
        adjusting it too would double-count and create the exact fake gap the
        adjustment exists to remove.
        """
        events = [{"ex_date": date("2024-01-03"), "factor_price": D("0.5")}]
        cf = cumulative_factors(events, DATES)
        assert cf[date("2024-01-01")] == D("0.5")
        assert cf[date("2024-01-02")] == D("0.5")
        assert cf[date("2024-01-03")] == D(1), "ex-date bar must NOT be adjusted"
        assert cf[date("2024-01-04")] == D(1)

    def test_most_recent_bar_is_always_unadjusted(self):
        """The right-edge identity: today's adjusted close == today's real close."""
        events = [
            {"ex_date": date("2024-01-02"), "factor_price": D("0.5")},
            {"ex_date": date("2024-01-03"), "factor_price": D("0.2")},
        ]
        cf = cumulative_factors(events, DATES)
        assert cf[max(DATES)] == D(1)

    def test_multiple_events_compound(self):
        """1:1 bonus then a 1:5 split: earliest bars carry 0.5 * 0.2 = 0.1."""
        events = [
            {"ex_date": date("2024-01-02"), "factor_price": D("0.5")},
            {"ex_date": date("2024-01-04"), "factor_price": D("0.2")},
        ]
        cf = cumulative_factors(events, DATES)
        assert cf[date("2024-01-01")] == D("0.1")
        assert cf[date("2024-01-02")] == D("0.2"), "only the later split applies"
        assert cf[date("2024-01-03")] == D("0.2")
        assert cf[date("2024-01-04")] == D(1)

    def test_event_order_in_input_does_not_matter(self):
        """Multiplication is commutative; the function must not depend on order."""
        a = [
            {"ex_date": date("2024-01-02"), "factor_price": D("0.5")},
            {"ex_date": date("2024-01-04"), "factor_price": D("0.2")},
        ]
        assert cumulative_factors(a, DATES) == cumulative_factors(list(reversed(a)), DATES)

    def test_two_events_on_the_same_ex_date_both_apply(self):
        """A bonus and a special dividend can genuinely share an ex-date."""
        events = [
            {"ex_date": date("2024-01-03"), "factor_price": D("0.5")},
            {"ex_date": date("2024-01-03"), "factor_price": D("0.9")},
        ]
        cf = cumulative_factors(events, DATES)
        assert cf[date("2024-01-01")] == D("0.45")

    def test_events_outside_the_date_range_still_apply(self):
        """An ex-date after the last bar must scale every bar in the window."""
        events = [{"ex_date": date("2025-06-01"), "factor_price": D("0.5")}]
        cf = cumulative_factors(events, DATES)
        assert set(cf.values()) == {D("0.5")}

    def test_events_before_the_window_are_ignored(self):
        events = [{"ex_date": date("2020-01-01"), "factor_price": D("0.5")}]
        cf = cumulative_factors(events, DATES)
        assert set(cf.values()) == {D(1)}

    def test_null_and_nonpositive_factors_are_skipped(self):
        """A pending or failed factor must not silently zero out the series."""
        events = [
            {"ex_date": date("2024-01-03"), "factor_price": None},
            {"ex_date": date("2024-01-03"), "factor_price": D(0)},
            {"ex_date": date("2024-01-03"), "factor_price": D("-1")},
        ]
        assert set(cumulative_factors(events, DATES).values()) == {D(1)}

    def test_empty_dates_returns_empty(self):
        assert cumulative_factors([{"ex_date": date("2024-01-01"), "factor_price": D("0.5")}], []) == {}


class TestVolumeFactors:
    def test_volume_moves_inversely(self):
        """A 1:1 bonus doubles pre-ex volume while halving pre-ex price."""
        events = [
            {"ex_date": date("2024-01-03"), "factor_price": D("0.5"), "factor_vol": D(2)}
        ]
        price = cumulative_factors(events, DATES, "factor_price")
        vol = cumulative_factors(events, DATES, "factor_vol")
        assert price[date("2024-01-01")] == D("0.5")
        assert vol[date("2024-01-01")] == D(2)


class TestTotalReturnFactors:
    def test_ordinary_dividend_only_affects_tr(self):
        events = build_tr_factors(
            [
                {
                    "type": "DIVIDEND",
                    "ex_date": date("2024-01-03"),
                    "factor_price": D(1),
                    "div_amount": D(4),
                    "anchor_price": D(100),
                }
            ]
        )
        assert events[0]["factor_tr"] == D("0.96")
        price = cumulative_factors(events, DATES, "factor_price")
        tr = cumulative_factors(events, DATES, "factor_tr")
        assert price[date("2024-01-01")] == D(1), "price series untouched by ordinary div"
        assert tr[date("2024-01-01")] == D("0.96")

    def test_split_affects_both_series_identically(self):
        """A split changes no wealth, so TR and price factors must match."""
        events = build_tr_factors(
            [{"type": "SPLIT", "ex_date": date("2024-01-03"), "factor_price": D("0.2")}]
        )
        assert events[0]["factor_tr"] == D("0.2")

    def test_special_dividend_affects_both(self):
        events = build_tr_factors(
            [
                {
                    "type": "SPECIAL_DIVIDEND",
                    "ex_date": date("2024-01-03"),
                    "factor_price": D("0.9"),
                    "div_amount": D(10),
                    "anchor_price": D(100),
                }
            ]
        )
        assert events[0]["factor_tr"] == D("0.9")

    def test_dividend_exceeding_price_is_ignored_not_negative(self):
        """Data error: must not produce a zero or negative TR factor."""
        events = build_tr_factors(
            [
                {
                    "type": "DIVIDEND",
                    "ex_date": date("2024-01-03"),
                    "factor_price": D(1),
                    "div_amount": D(500),
                    "anchor_price": D(100),
                }
            ]
        )
        assert events[0]["factor_tr"] == D(1)

    def test_dividend_without_anchor_leaves_tr_neutral(self):
        events = build_tr_factors(
            [
                {
                    "type": "DIVIDEND",
                    "ex_date": date("2024-01-03"),
                    "factor_price": D(1),
                    "div_amount": D(4),
                    "anchor_price": None,
                }
            ]
        )
        assert events[0]["factor_tr"] == D(1)

    def test_build_tr_does_not_mutate_input(self):
        src = [{"type": "SPLIT", "ex_date": date("2024-01-03"), "factor_price": D("0.2")}]
        build_tr_factors(src)
        assert "factor_tr" not in src[0]


class TestFrame:
    def test_frame_has_all_three_series(self):
        events = build_tr_factors(
            [
                {
                    "type": "DIVIDEND",
                    "ex_date": date("2024-01-03"),
                    "factor_price": D(1),
                    "factor_vol": D(1),
                    "div_amount": D(4),
                    "anchor_price": D(100),
                }
            ]
        )
        f = cumulative_factor_frame(events, DATES)
        assert set(f.columns) == {"trade_date", "cum_factor", "cum_factor_vol", "cum_factor_tr"}
        assert len(f) == len(DATES)
        row = f.filter(f["trade_date"] == date("2024-01-01")).to_dicts()[0]
        assert row["cum_factor"] == pytest.approx(1.0)
        assert row["cum_factor_tr"] == pytest.approx(0.96)


class TestGoldenSplitContinuity:
    """The acceptance criterion: no fake gap on the ex-date.

    A 1:1 bonus makes the raw series look like a 50 % overnight crash. After
    adjustment the return across the ex-date must be near zero.
    """

    def test_bonus_removes_the_fake_gap(self):
        raw_closes = {
            date("2024-01-01"): 200.0,
            date("2024-01-02"): 202.0,
            date("2024-01-03"): 101.0,  # ex-date: price halves mechanically
            date("2024-01-04"): 103.0,
        }
        events = [{"ex_date": date("2024-01-03"), "factor_price": D("0.5")}]
        cf = cumulative_factors(events, list(raw_closes))

        adj = {d: c * float(cf[d]) for d, c in raw_closes.items()}

        raw_ret = raw_closes[date("2024-01-03")] / raw_closes[date("2024-01-02")] - 1
        adj_ret = adj[date("2024-01-03")] / adj[date("2024-01-02")] - 1

        assert raw_ret == pytest.approx(-0.5, abs=0.01), "raw shows a fake 50 % crash"
        assert abs(adj_ret) < 0.01, f"adjusted must be continuous, got {adj_ret:.4f}"

    def test_adjusted_series_is_monotone_in_the_right_direction(self):
        """Adjusted history must be scaled DOWN, never up, by a dilutive event."""
        events = [{"ex_date": date("2024-01-03"), "factor_price": D("0.5")}]
        cf = cumulative_factors(events, DATES)
        assert cf[date("2024-01-01")] < cf[date("2024-01-04")]
