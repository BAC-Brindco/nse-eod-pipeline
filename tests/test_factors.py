"""Hand-checked factor formula tests.

Every expected value below was computed by hand from the Bloomberg DPDF
definitions, not by running the code and pasting the output.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nse_eod.transform import factors as F


def D(x) -> Decimal:
    return Decimal(str(x))


# --------------------------------------------------------------------- bonus
class TestBonus:
    def test_one_for_one_halves_price_and_doubles_volume(self):
        """A 1:1 bonus: 1 share becomes 2. Pre-ex prices halve, volume doubles."""
        f = F.bonus_factor(1, 1)
        assert f.factor_price == D("0.5")
        assert f.factor_vol == D("2")
        assert f.state == F.COMPUTED

    def test_one_for_one_on_real_numbers(self):
        """Rs 100 pre-ex close becomes Rs 50 adjusted: continuous across a 1:1."""
        f = F.bonus_factor(1, 1)
        assert D(100) * f.factor_price == D(50)
        assert D(1000) * f.factor_vol == D(2000)

    def test_three_for_one(self):
        """Bonus 3:1 -> before 1, after 4 -> 1/4."""
        f = F.bonus_factor(3, 1)
        assert f.factor_price == D("0.25")
        assert f.factor_vol == D("4")

    def test_thirteen_for_ten(self):
        """Bonus 13:10 -> before 10, after 23 -> 10/23. A real census ratio."""
        f = F.bonus_factor(13, 10)
        assert f.factor_price == D(10) / D(23)
        assert f.factor_vol == D(23) / D(10)

    def test_one_for_three(self):
        """Bonus 1:3 -> before 3, after 4 -> 0.75."""
        assert F.bonus_factor(1, 3).factor_price == D("0.75")

    def test_price_times_volume_is_invariant(self):
        """Turnover is preserved: factor_price * factor_vol == 1.

        Exact equality is unavailable for non-terminating ratios (10/23 * 23/10
        is 1 only in the limit of infinite precision), so this asserts to within
        Decimal's working precision. Turnover invariance is the property that
        matters; bit-exactness is not achievable and not required.
        """
        for num, den in [(1, 1), (3, 1), (13, 10), (1, 3), (4, 1), (1, 116)]:
            f = F.bonus_factor(num, den)
            assert abs(f.factor_price * f.factor_vol - D(1)) < D("1e-25")

    @pytest.mark.parametrize("num,den", [(0, 1), (1, 0), (-1, 2), (2, -1)])
    def test_rejects_nonpositive_ratio(self, num, den):
        with pytest.raises(F.FactorError):
            F.bonus_factor(num, den)


# --------------------------------------------------------------------- split
class TestSplit:
    def test_ten_to_two_is_one_fifth(self):
        """Face Value Split From Rs 10 To Rs 2: price x0.2, volume x5."""
        f = F.split_factor(10, 2)
        assert f.factor_price == D("0.2")
        assert f.factor_vol == D("5")

    def test_one_for_two_split(self):
        """A 1:2 split (Rs 10 -> Rs 5) halves pre-ex prices."""
        f = F.split_factor(10, 5)
        assert f.factor_price == D("0.5")
        assert f.factor_vol == D("2")

    def test_ten_to_one(self):
        f = F.split_factor(10, 1)
        assert f.factor_price == D("0.1")
        assert f.factor_vol == D("10")

    def test_four_to_two(self):
        """A real census row: From Rs 4/- To Rs 2/-."""
        assert F.split_factor(4, 2).factor_price == D("0.5")

    def test_reverse_split_gives_factor_above_one(self):
        """Consolidation Rs 1 -> Rs 10: pre-ex prices must be multiplied UP."""
        f = F.split_factor(1, 10)
        assert f.factor_price == D("10")
        assert f.factor_vol == D("0.1")

    def test_split_and_bonus_share_one_primitive(self):
        """A 1:1 bonus and a Rs 10->Rs 5 split are the same economic event."""
        assert F.bonus_factor(1, 1).factor_price == F.split_factor(10, 5).factor_price

    @pytest.mark.parametrize("old,new", [(0, 2), (10, 0), (-10, 2)])
    def test_rejects_nonpositive_face_value(self, old, new):
        with pytest.raises(F.FactorError):
            F.split_factor(old, new)


# ------------------------------------------------------------- special cash
class TestCashDividend:
    def test_special_dividend_reduces_price_proportionally(self):
        """P=100, C=10 -> (100-10)/100 = 0.9."""
        f = F.cash_factor(100, 10)
        assert f.factor_price == D("0.9")
        assert f.factor_vol == D(1), "a cash event must not touch volume"

    def test_real_census_amount(self):
        """Special Dividend - Rs 7.50 on a Rs 250 close -> 0.97."""
        f = F.cash_factor(250, "7.50")
        assert f.factor_price == D("0.97")

    def test_zero_cash_is_neutral(self):
        assert F.cash_factor(100, 0).factor_price == D(1)

    def test_rejects_cash_at_or_above_price(self):
        """Would drive the adjusted series to zero or negative."""
        with pytest.raises(F.FactorError):
            F.cash_factor(10, 10)
        with pytest.raises(F.FactorError):
            F.cash_factor(10, 12)

    def test_rejects_nonpositive_price(self):
        with pytest.raises(F.FactorError):
            F.cash_factor(0, 1)


# -------------------------------------------------------------------- rights
class TestRights:
    def test_one_for_one_at_half_price(self):
        """N=1, S=50, P=100 -> (1 + 1*0.5)/2 = 0.75."""
        f = F.rights_factor(1, 1, 50, 100)
        assert f.factor_price == D("0.75")
        assert f.factor_vol == D(1)
        assert f.state == F.COMPUTED

    def test_one_for_five(self):
        """N=0.2, S=50, P=100 -> (1 + 0.2*0.5)/1.2 = 1.1/1.2."""
        f = F.rights_factor(1, 5, 50, 100)
        assert f.factor_price == D("1.1") / D("1.2")

    def test_rights_at_par_with_zero_premium(self):
        """The census case 'Rights 1:1 @ Premium Rs 0/-' with faceVal 5.

        All-in S = face_value + premium = 5 + 0 = 5, NOT 0. With P=50:
        (1 + 1*(5/50))/2 = 1.1/2 = 0.55.
        Had the premium been used as S, the factor would be 1/2 = 0.5 -- a
        fabricated extra 5 % drop on every pre-ex bar.
        """
            # face_value + premium
        s = D(5) + D(0)
        f = F.rights_factor(1, 1, s, 50)
        assert f.factor_price == D("0.55")
        assert f.factor_price != D("0.5")

    def test_premium_must_be_added_to_face_value(self):
        """'Rights 1:1 @ Premium Rs 11.25/-' with faceVal 5 -> S = 16.25.

        P = 25: (1 + 1*(16.25/25))/2 = (1 + 0.65)/2 = 0.825.
        Using the bare premium 11.25 would give (1+0.45)/2 = 0.725 -- 12 % too low.
        """
        s_correct = D(5) + D("11.25")
        assert F.rights_factor(1, 1, s_correct, 25).factor_price == D("0.825")
        assert F.rights_factor(1, 1, D("11.25"), 25).factor_price == D("0.725")

    def test_skipped_when_subscription_above_market(self):
        """S > P: Bloomberg does not calculate. Neutral factor, explicit state."""
        f = F.rights_factor(1, 1, 150, 100)
        assert f.factor_price == D(1)
        assert f.factor_vol == D(1)
        assert f.state == F.SKIPPED_S_GT_P
        assert not f.adjusts_price

    def test_s_equal_to_p_is_computed_and_neutral(self):
        """S == P is not 'skipped'; the formula legitimately yields exactly 1."""
        f = F.rights_factor(1, 1, 100, 100)
        assert f.state == F.COMPUTED
        assert f.factor_price == D(1)

    def test_fractional_ratio_denominator(self):
        """Census has 'Rights 1:11.10'. Truncating to 11 changes N by ~1 %."""
        f_exact = F.rights_factor(1, D("11.10"), 50, 100)
        f_trunc = F.rights_factor(1, 11, 50, 100)
        assert f_exact.factor_price != f_trunc.factor_price

    def test_deep_discount_rights_is_heavily_dilutive(self):
        """N=1, S=1, P=100 -> (1 + 0.01)/2 = 0.505."""
        assert F.rights_factor(1, 1, 1, 100).factor_price == D("0.505")


# ------------------------------------------------------------------ spin-off
class TestSpinoff:
    def test_single_spinoff(self):
        """B=20, N=0.5, P=100 -> 1 - 10/100 = 0.9."""
        assert F.spinoff_factor(100, 20, "0.5").factor_price == D("0.9")

    def test_multi_spinoff_bloomberg_example(self):
        """Bloomberg's RMX CN worked example, with its own total corrected.

        The DPDF doc lists PGR CN = 0.0999999 and ARL CN = 0.4625 and then states
        the total as 0.562499. The true sum is 0.5624999 -- the doc rounds. We
        sum the legs rather than trusting the printed total, so with P = 10:
            V = 0.5624999 ; factor = 1 - 0.05624999 = 0.94375001
        """
        legs = [("0.0999999", 1), ("0.4625", 1)]
        v = D("0.0999999") + D("0.4625")
        assert v == D("0.5624999"), "the legs sum to this, not the doc's 0.562499"
        f = F.multi_spinoff_factor(10, legs)
        assert f.factor_price == D(1) - v / D(10)
        assert f.factor_price == D("0.94375001")

    def test_rejects_value_above_parent_price(self):
        with pytest.raises(F.FactorError):
            F.spinoff_factor(100, 200, 1)


# ------------------------------------------------------------- abnormal cash
class TestAbnormalCash:
    def test_bloomberg_abnormal_cash(self):
        """P=100, RC=2, SP=10 -> (100-2-10)/(100-2) = 88/98."""
        f = F.abnormal_cash_factor(100, 2, 10)
        assert f.factor_price == D(88) / D(98)

    def test_no_special_component_is_neutral(self):
        """An ordinary dividend alone must not move the price series."""
        assert F.abnormal_cash_factor(100, 5, 0).factor_price == D(1)

    def test_reduces_to_plain_cash_when_no_regular_dividend(self):
        assert (
            F.abnormal_cash_factor(100, 0, 10).factor_price
            == F.cash_factor(100, 10).factor_price
        )


# ------------------------------------------------------------- total return
class TestTotalReturn:
    def test_ordinary_dividend_moves_tr_but_not_price(self):
        """The whole point of the parallel series."""
        price = F.compute_factor("DIVIDEND", div_amount=4, anchor_price=100)
        tr = F.total_return_factor(100, 4)
        assert price.factor_price == D(1), "ordinary dividend must NOT adjust price"
        assert tr.factor_price == D("0.96"), "but it must adjust total return"


# ---------------------------------------------------------------- dispatch
class TestComputeFactorDispatch:
    def test_ordinary_dividend_is_neutral(self):
        f = F.compute_factor("DIVIDEND", div_amount=4, anchor_price=100)
        assert f.factor_price == D(1)
        assert f.state == F.NOT_APPLICABLE

    def test_interim_dividend_is_neutral(self):
        assert F.compute_factor("INTERIM_DIVIDEND", div_amount=9).factor_price == D(1)

    def test_agm_is_neutral(self):
        assert F.compute_factor("AGM").factor_price == D(1)

    def test_preference_bonus_never_adjusts_equity_price(self):
        """The NCRPS trap. 'Bonus Ncrps 4:1' must leave equity prices alone.

        If this ever returns 0.2, four fifths of a real symbol's price history
        has just been destroyed.
        """
        f = F.compute_factor("BONUS_PREFERENCE", ratio_num=4, ratio_den=1)
        assert f.factor_price == D(1)
        assert f.state == F.NOT_APPLICABLE
        assert not f.adjusts_price

    def test_demerger_is_unresolved_not_silently_neutral(self):
        """Factor is 1.0 so nothing breaks, but the state marks it unresolved."""
        f = F.compute_factor("DEMERGER")
        assert f.factor_price == D(1)
        assert f.state == F.UNRESOLVED

    def test_bonus_via_dispatch(self):
        assert F.compute_factor("BONUS", ratio_num=1, ratio_den=1).factor_price == D("0.5")

    def test_split_via_dispatch(self):
        assert F.compute_factor("SPLIT", ratio_num=10, ratio_den=2).factor_price == D("0.2")

    def test_rights_via_dispatch(self):
        f = F.compute_factor("RIGHTS", ratio_num=1, ratio_den=1, sub_price=50, anchor_price=100)
        assert f.factor_price == D("0.75")

    def test_special_dividend_via_dispatch(self):
        f = F.compute_factor("SPECIAL_DIVIDEND", div_amount=10, anchor_price=100)
        assert f.factor_price == D("0.9")

    def test_missing_anchor_defers_rather_than_guessing(self):
        """No P available -> needs_anchor, factor 1.0, retried on a later run."""
        f = F.compute_factor("SPECIAL_DIVIDEND", div_amount=10, anchor_price=None)
        assert f.state == F.NEEDS_ANCHOR
        assert f.factor_price == D(1)

        f = F.compute_factor("RIGHTS", ratio_num=1, ratio_den=1, sub_price=50)
        assert f.state == F.NEEDS_ANCHOR

    def test_return_of_capital_adjusts_price(self):
        f = F.compute_factor("RETURN_OF_CAPITAL", div_amount=5, anchor_price=100)
        assert f.factor_price == D("0.95")


# ------------------------------------------------------------------ guardrails
class TestGuardrails:
    def test_absurd_factor_is_rejected(self):
        """A 1:1000000 'split' is a parse error, not a corporate action."""
        with pytest.raises(F.FactorError):
            F.split_factor(1, 10_000_000)

    def test_non_numeric_input_is_rejected(self):
        with pytest.raises(F.FactorError):
            F.bonus_factor("one", 1)
