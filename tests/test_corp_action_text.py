"""Corporate-action free-text parser tests.

Every literal string below is a REAL NSE ``subject`` value taken from the 8,543-row
census in ``fixtures/corp_actions_real_2023_2026.json``. No invented inputs.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from nse_eod.parsers.corp_action_text import (
    AGM,
    BONUS,
    BONUS_PREFERENCE,
    BUYBACK,
    DEMERGER,
    DIVIDEND,
    INTEREST_PAYMENT,
    INTERIM_DIVIDEND,
    NEEDS_EXTERNAL,
    PRICE_ADJUSTING,
    RETURN_OF_CAPITAL,
    RIGHTS,
    SPECIAL_DIVIDEND,
    SPLIT,
    UNKNOWN,
    parse_purpose,
    review_severity,
    split_components,
)

FIXTURES = Path(__file__).parent / "fixtures"
CENSUS = FIXTURES / "corp_actions_real_2023_2026.json"


def D(x):
    return Decimal(str(x))


def one(purpose: str, face_value=None):
    """Parse and assert exactly one component came back."""
    r = parse_purpose(purpose, face_value)
    assert len(r.components) == 1, f"expected 1 component, got {[c.type for c in r.components]}"
    return r.components[0]


# ------------------------------------------------------------------- bonus
class TestBonus:
    @pytest.mark.parametrize(
        "text,num,den",
        [
            ("Bonus 1:1", 1, 1),
            ("Bonus 3:1", 3, 1),
            ("Bonus 4:1", 4, 1),
            ("Bonus 13:10", 13, 10),
            ("Bonus 1:3", 1, 3),
            ("Bonus 2:5", 2, 5),
        ],
    )
    def test_real_bonus_ratios(self, text, num, den):
        c = one(text)
        assert c.type == BONUS
        assert c.parsed_ok
        assert c.ratio_num == D(num)
        assert c.ratio_den == D(den)

    def test_bonus_with_no_ratio_goes_to_review(self):
        c = one("Bonus Issue")
        assert c.type == BONUS
        assert not c.parsed_ok
        assert c.needs_review
        assert review_severity(c) == "high", "an unparsed bonus is price-affecting"


class TestNCRPSTrap:
    """The single most dangerous string in the feed.

    'Bonus Ncrps 4:1' is a bonus of non-convertible redeemable PREFERENCE shares.
    The equity share count does not change. Reading it as a 4:1 equity bonus
    applies a 0.2 factor and destroys 80 % of that symbol's price history.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Bonus Ncrps 1:116",
            "Scheme Of Arrangement - Bonus Ncrps 1:10",
            "Scheme Of Arrangement - Bonus Ncrps 4:1",
            "Scheme Of Arrangement - Bonus Ncrps 3:1",
        ],
    )
    def test_ncrps_is_never_an_equity_bonus(self, text):
        r = parse_purpose(text)
        types = {c.type for c in r.components}
        assert BONUS not in types, f"{text!r} must NOT be read as an equity bonus"
        assert BONUS_PREFERENCE in types
        assert not r.any_price_adjusting, "preference bonus must not adjust the equity price"

    def test_ncrps_ordering_beats_a_permissive_bonus_regex(self):
        """Any permissive 'bonus ... ratio' extraction reads 4:1 out of this string.

        That is the hazard: the word 'bonus' and a ratio are both present, so a
        parser that looks for them independently -- as most would -- produces a
        4:1 equity bonus. Only an explicit preference-share rule ahead of the
        bonus rule prevents it.
        """
        assert re.search(r"\bbonus\b.*?(\d+)\s*:\s*(\d+)", "Bonus Ncrps 4:1", re.I)
        assert one("Bonus Ncrps 4:1").type == BONUS_PREFERENCE

    def test_ccps_rights_is_also_not_equity(self):
        r = parse_purpose("Rights - 7 Ccps And 7 Warrants:40")
        assert BONUS_PREFERENCE in {c.type for c in r.components} or not r.any_price_adjusting


# ------------------------------------------------------------------- splits
class TestSplit:
    @pytest.mark.parametrize(
        "text,old,new",
        [
            ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 10, 2),
            ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", 10, 1),
            ("Face Value Split (Sub-Division) - From Rs 4/- Per Share To Rs 2/- Per Share", 4, 2),
            ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share", 10, 5),
            ("Face Value Split (Sub-Division) - From Rs2/- Per Share To Re 1/- Per Share", 2, 1),
        ],
    )
    def test_real_split_strings(self, text, old, new):
        c = one(text)
        assert c.type == SPLIT
        assert c.parsed_ok
        assert c.ratio_num == D(old), "ratio_num is the OLD face value"
        assert c.ratio_den == D(new), "ratio_den is the NEW face value"

    def test_re_variant_for_sub_rupee_face_value(self):
        """'To Re 1' (not 'Rs 1') is used for sub-rupee-or-one values."""
        c = one("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share")
        assert c.ratio_den == D(1)


# ------------------------------------------------------------------- rights
class TestRights:
    @pytest.mark.parametrize(
        "text,fv,num,den,premium,sub_price",
        [
            # (text, faceVal, N_num, N_den, printed premium, all-in S)
            ("Rights 1:1 @ Premium Rs 11.25/-", 5, 1, 1, "11.25", "16.25"),
            ("Rights 2:5@ Premium Rs 2/-", 10, 2, 5, 2, 12),
            ("Rights 2:17 @ Premium Rs 545/-", 10, 2, 17, 545, 555),
            ("Rights 1:2@ Premium Rs 11.60/-", 1, 1, 2, "11.60", "12.60"),
            ("Rights 11:64 @ Premium Rs 473/-", 1, 11, 64, 473, 474),
            ("Rights 3:19 @ Premium Rs 74/-", 1, 3, 19, 74, 75),
            ("Rights 7:40 @ Premium Rs 254/-", 10, 7, 40, 254, 264),
            ("Rights 1:9 @ Premium 91", 10, 1, 9, 91, 101),
            ("Rights 3:2 @ Prm Rs 0.63/-", 1, 3, 2, "0.63", "1.63"),
        ],
    )
    def test_subscription_price_is_face_value_plus_premium(
        self, text, fv, num, den, premium, sub_price
    ):
        """NSE prints the PREMIUM, not the subscription price.

        Getting this wrong understates S and so overstates the dilution on every
        pre-ex bar.
        """
        c = one(text, fv)
        assert c.type == RIGHTS
        assert c.parsed_ok, f"{text!r} should parse"
        assert c.ratio_num == D(num)
        assert c.ratio_den == D(den)
        assert c.premium == D(premium)
        assert c.sub_price == D(sub_price), "S must be face_value + premium"

    @pytest.mark.parametrize(
        "text,fv,expected_s",
        [
            ("Rights 1:1 @ Premium Rs 0/-", 5, 5),
            ("Rights 2:53 @ Premium Rs 0/-", 10, 10),
            ("Rights 10:13 @ Premium Rs 0/-", 1, 1),
        ],
    )
    def test_zero_premium_means_issued_at_par(self, text, fv, expected_s):
        """'@ Premium Rs 0/-' is an issue AT PAR: S = face value, never 0.

        Ten of the 147 real rights rows look like this. Treating S as 0 turns the
        factor into 1/(1+N) -- a fabricated crash on every pre-ex bar.
        """
        c = one(text, fv)
        assert c.sub_price == D(expected_s)
        assert c.sub_price != D(0)

    def test_fractional_ratio_is_not_truncated(self):
        """'Rights 1:11.10' -- capturing only \\d+ would silently make it 1:11."""
        c = one("Rights 1:11.10 @ Premium Rs 0/-", 100)
        assert c.ratio_den == D("11.10")
        assert c.ratio_den != D(11)

    def test_missing_face_value_is_reviewed_not_guessed(self):
        c = one("Rights 1:1 @ Premium Rs 11.25/-", None)
        assert not c.parsed_ok
        assert review_severity(c) == "high"

    def test_no_premium_printed_is_reviewed(self):
        """Without a premium we cannot form S; face value alone is a guess."""
        c = one("Rights 10:63", 100)
        assert c.type == RIGHTS
        assert not c.parsed_ok
        assert c.review_reason == "ambiguous_ratio"

    def test_rights_with_warrants_is_reviewed(self):
        """The warrant leg changes the economics and its terms are not given."""
        c = one("Rights 1:50 @ Premium Rs 690/- With 17 Warrants For 50 Equity Shares", 2)
        assert c.type == RIGHTS
        assert not c.parsed_ok
        assert review_severity(c) == "high"


# --------------------------------------------------------------- dividends
class TestDividends:
    @pytest.mark.parametrize(
        "text,etype,amount",
        [
            ("Dividend - Rs 16 Per Share", DIVIDEND, 16),
            ("Dividend - Rs 1.50 Per Share", DIVIDEND, "1.50"),
            ("Dividend - Re 0.40 Per Share", DIVIDEND, "0.40"),
            ("Dividend - Re 0.05 Per Share", DIVIDEND, "0.05"),
            ("Dividend - Rs 2.5 Per Share", DIVIDEND, "2.5"),
            ("Dividend - Rs 4 Per Sh", DIVIDEND, 4),
            ("Dividend - Rs5 Per Share", DIVIDEND, 5),
            ("Interim Dividend - Rs 8 Per Share", INTERIM_DIVIDEND, 8),
            ("Interim Dividend - Re 1 Per Sh", INTERIM_DIVIDEND, 1),
            ("Special Dividend - Rs 7.50 Per Share", SPECIAL_DIVIDEND, "7.50"),
            ("Special Dividend - Rs 11 Per Share", SPECIAL_DIVIDEND, 11),
        ],
    )
    def test_real_dividend_strings(self, text, etype, amount):
        c = one(text)
        assert c.type == etype
        assert c.parsed_ok
        assert c.div_amount == D(amount)

    def test_re_vs_rs_both_handled(self):
        """~1,300 census rows use 'Re' for sub-rupee amounts."""
        assert one("Dividend - Re 0.40 Per Share").div_amount == D("0.40")
        assert one("Dividend - Rs 40 Per Share").div_amount == D(40)

    def test_leading_zero_amount(self):
        assert one("Interim Dividend - Rs 09 Per Share").div_amount == D(9)

    def test_ordinary_dividend_does_not_adjust_price(self):
        r = parse_purpose("Dividend - Rs 16 Per Share")
        assert not r.any_price_adjusting

    def test_special_dividend_does_adjust_price(self):
        r = parse_purpose("Special Dividend - Rs 10 Per Share")
        assert r.any_price_adjusting
        assert r.components[0].is_special


# ------------------------------------------------------- compound purposes
class TestCompoundPurposes:
    def test_agm_slash_dividend(self):
        r = parse_purpose("Annual General Meeting/Dividend - Rs 5 Per Share")
        assert [c.type for c in r.components] == [AGM, DIVIDEND]
        assert r.components[1].div_amount == D(5)

    def test_three_components_with_a_special(self):
        r = parse_purpose(
            "Annual General Meeting/Dividend - Rs 5 Per Share/Special Dividend - Rs 10 Per Share"
        )
        types = [c.type for c in r.components]
        assert types == [AGM, DIVIDEND, SPECIAL_DIVIDEND]
        assert r.any_price_adjusting, "the special component must still adjust price"
        assert r.components[2].div_amount == D(10)

    def test_ampersand_separator(self):
        r = parse_purpose("Dividend - Rs 4 Per Share & Special Dividend Rs 10 Per Share")
        assert {c.type for c in r.components} == {DIVIDEND, SPECIAL_DIVIDEND}

    def test_missing_separator_between_dividends(self):
        """Real string: 'Interim Dividend - Rs 8 Per Share Special Dividend - Rs 67 Per Share'."""
        r = parse_purpose("Interim Dividend - Rs 8 Per Share Special Dividend - Rs 67 Per Share")
        types = [c.type for c in r.components]
        assert INTERIM_DIVIDEND in types
        assert SPECIAL_DIVIDEND in types
        amounts = {c.div_amount for c in r.components if c.div_amount}
        assert amounts == {D(8), D(67)}

    def test_nse_typo_meeting_glued_to_dividend(self):
        """'Annual General Meetingdividend - Rs 6 Per Share' -- 9 real rows."""
        r = parse_purpose("Annual General Meetingdividend - Rs  6 Per Share")
        types = [c.type for c in r.components]
        assert AGM in types
        assert DIVIDEND in types
        assert any(c.div_amount == D(6) for c in r.components)

    def test_interim_glued_to_dividend(self):
        r = parse_purpose("Interimdividend - Re 1 Per Share")
        assert r.components[0].type == INTERIM_DIVIDEND
        assert r.components[0].div_amount == D(1)

    def test_component_indices_are_sequential(self):
        r = parse_purpose(
            "Annual General Meeting/Dividend - Rs 5 Per Share/Special Dividend - Rs 10 Per Share"
        )
        assert [c.component_ix for c in r.components] == [0, 1, 2]

    def test_event_uids_are_distinct_within_a_compound(self):
        """Otherwise the components would overwrite each other on upsert."""
        r = parse_purpose(
            "Annual General Meeting/Dividend - Rs 5 Per Share/Special Dividend - Rs 10 Per Share"
        )
        uids = {c.event_uid("INE123A0101", "2026-01-01") for c in r.components}
        assert len(uids) == 3

    def test_event_uid_is_deterministic(self):
        a = one("Bonus 1:1").event_uid("INE123A0101", "2026-01-01")
        b = one("Bonus 1:1").event_uid("INE123A0101", "2026-01-01")
        assert a == b


class TestLabelToRatioGap:
    """Regressions for real 2020-2026 rows the v1.0.0 parser silently missed.

    Each of these left a symbol's price history WRONG until v1.1.0. They were
    found by sweeping every stored announcement mentioning bonus/split/rights
    that produced no parsed price-adjusting event.
    """

    def test_bonus_with_punctuation_before_the_ratio(self):
        """AJANTPHARM 2022-06-22 'Bonus- 1:2'.

        A `\\s*`-only gap between the label and the ratio missed this, leaving
        Ajanta Pharma's entire pre-2022 history unadjusted by a third.
        """
        c = one("Bonus- 1:2", 2)
        assert c.type == BONUS
        assert c.parsed_ok
        assert (c.ratio_num, c.ratio_den) == (D(1), D(2))

    @pytest.mark.parametrize(
        "text,fv,num,den,s",
        [
            # ASIANTILES 2022-04-11 and BHAGCHEM 2022-04-07: the word "Issue"
            # sat between the label and the ratio.
            ("Rights Issue 30:37@ Premium Rs 53/-", 10, 30, 37, 63),
            ("Rights Issue 4:17@ Premium Rs 390/-", 1, 4, 17, 391),
        ],
    )
    def test_rights_issue_wording(self, text, fv, num, den, s):
        c = one(text, fv)
        assert c.type == RIGHTS
        assert c.parsed_ok
        assert (c.ratio_num, c.ratio_den) == (D(num), D(den))
        assert c.sub_price == D(s)

    def test_bonus_paid_in_debentures_is_not_an_equity_bonus(self):
        """BRITANNIA 2021-05-25: 1 debenture per share held.

        Share count unchanged, but value IS distributed, and the debenture's
        value is not in the text — so it needs external data, at HIGH severity.
        Typing it as an unparsed BONUS filed it as low and would have buried it.
        """
        c = one("Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held", 1)
        assert c.type != BONUS
        assert not c.parsed_ok
        assert c.review_reason == "needs_external_price"
        assert review_severity(c) == "high"

    def test_partly_paid_rights_is_not_computed(self):
        """ABFRL 2020-06-30 'Rights 9:77 Partly Paid @ Premium Rs 100/-'.

        Only the first call is paid at ex-date, so the printed premium overstates
        the effective subscription price.
        """
        c = one("Rights 9:77 Partly Paid @ Premium Rs 100/-", 10)
        assert c.type == RIGHTS
        assert not c.parsed_ok
        assert review_severity(c) == "high"

    def test_severity_follows_the_reason_not_only_the_type(self):
        c = one("Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held", 1)
        assert c.type not in PRICE_ADJUSTING
        assert review_severity(c) == "high", "needs_external_price must always be high"


class TestSpellingVariants:
    """NSE misspellings from the 2020-2026 history.

    These only affect the total-return leg (dividends do not move the price
    series), so a miss silently understates TR rather than breaking prices.
    """

    @pytest.mark.parametrize(
        "text,etype,amount",
        [
            ("Divdend - Rs 0.50 Per Share", DIVIDEND, "0.50"),
            ("Interim Divdend - Rs 2 Per Share", INTERIM_DIVIDEND, 2),
            ("Interim Dividned - Rs 135 Per Share", INTERIM_DIVIDEND, 135),
            ("Div - Rs 0.50 Per Sh", DIVIDEND, "0.50"),
            ("Int Div-Rs 0.5 Per Sh", DIVIDEND, "0.5"),
        ],
    )
    def test_dividend_misspellings(self, text, etype, amount):
        c = one(text)
        assert c.type == etype
        assert c.parsed_ok
        assert c.div_amount == D(amount)

    @pytest.mark.parametrize(
        "text,etype",
        [
            ("Annual General Meetin", AGM),
            ("Extra-Ordinary General Meting", "EGM"),
            ("Nterest Amount- Rs 3.0556", INTEREST_PAYMENT),
        ],
    )
    def test_label_misspellings(self, text, etype):
        c = one(text)
        assert c.type == etype
        assert c.parsed_ok
        assert not c.needs_review

    def test_principal_debt_repayment_is_capital_return(self):
        c = one("Principal Debt Repayment - Rs 9.9905 Per Unit", 100)
        assert c.type == RETURN_OF_CAPITAL
        assert c.div_amount == D("9.9905")


class TestSplitterSafety:
    def test_does_not_split_on_thousands_separator(self):
        """Splitting on ',' would shred 'Rs 1,250'."""
        c = one("Dividend - Rs 1,250 Per Share")
        assert c.div_amount == D(1250)

    def test_does_not_split_on_the_dash_of_a_label(self):
        assert len(split_components("Dividend - Rs 4 Per Share")) == 1

    def test_protects_trailing_slash_dash(self):
        """'Rs 74/-' must not split into 'Rs 74' and '-'."""
        parts = split_components("Rights 3:19 @ Premium Rs 74/-")
        assert len(parts) == 1
        assert "74" in parts[0]

    def test_does_not_split_a_ratio(self):
        assert len(split_components("Bonus 1:1")) == 1


# ------------------------------------------------ non-adjusting recognition
class TestNonAdjusting:
    @pytest.mark.parametrize(
        "text,etype",
        [
            ("Annual General Meeting", AGM),
            ("Extra Ordinary General Meeting", "EGM"),
            ("Extra-Ordinary General Meeting", "EGM"),
            ("Interest Payment", INTEREST_PAYMENT),
            ("Buy Back", BUYBACK),
            ("Buyback", BUYBACK),
        ],
    )
    def test_recognised_with_no_price_effect(self, text, etype):
        """These are high-volume (AGM alone is 1,550 of 8,543 census rows).

        Typing them explicitly is what keeps the review queue small enough that
        a human will actually work it.
        """
        c = one(text)
        assert c.type == etype
        assert c.parsed_ok
        assert not c.needs_review, "must NOT flood the review queue"

    def test_agm_alone_does_not_adjust(self):
        assert not parse_purpose("Annual General Meeting").any_price_adjusting


class TestNeedsExternalData:
    @pytest.mark.parametrize(
        "text,etype",
        [("Demerger", DEMERGER), ("Capital Reduction", "CAPITAL_REDUCTION"),
         ("Redemption", "REDEMPTION")],
    )
    def test_typed_and_flagged_high_never_guessed(self, text, etype):
        c = one(text)
        assert c.type == etype
        assert not c.parsed_ok
        assert c.review_reason == "needs_external_price"
        assert review_severity(c) == "high"

    def test_demerger_is_not_price_adjusting_without_data(self):
        """Bloomberg's 1-(B*N)/P needs the spun-off close, which NSE EOD lacks."""
        assert not parse_purpose("Demerger").any_price_adjusting


# ------------------------------------------------------ REIT / InvIT units
class TestUnitDistributions:
    def test_return_of_capital_adjusts_price(self):
        c = one("Return Of Capital - Re 0.3848 Per Unit")
        assert c.type == RETURN_OF_CAPITAL
        assert c.div_amount == D("0.3848")

    def test_amount_before_the_label(self):
        c = one("Rs 1.4534 Per Unit As Return Of Capital")
        assert c.type == RETURN_OF_CAPITAL
        assert c.div_amount == D("1.4534")

    def test_typo_variants_from_the_census(self):
        for text in ("Retun Of Capital Rs 1.2 Per Unit", "Re 0.5 Return On Captial Per Unit"):
            c = one(text)
            assert c.type == RETURN_OF_CAPITAL, text

    def test_spv_debt_repayment_is_capital_return(self):
        c = one("Repayment Of Spv Debt - Rs 1.5 Per Unit")
        assert c.type == RETURN_OF_CAPITAL
        assert c.div_amount == D("1.5")

    def test_distribution_headline_is_not_double_counted(self):
        """The leading total is the SUM of the parts, not an extra payment."""
        r = parse_purpose(
            "Distribution - Rs 1.6876 Per Unit Consists Of Dividend Re 0.2958 Per Unit/ "
            "Interest - Re 0.7046 Per Unit/ Return Of Capital - Re 0.6872 Per Unit"
        )
        amounts = [c.div_amount for c in r.components if c.div_amount]
        assert D("1.6876") not in amounts, "headline total must be neutralised"
        roc = [c for c in r.components if c.type == RETURN_OF_CAPITAL]
        assert len(roc) == 1
        assert roc[0].div_amount == D("0.6872")

    def test_unit_income_does_not_adjust_price(self):
        assert one("Treasury Income Re 0.5 Per Unit").type == INTEREST_PAYMENT
        assert one("Interest Amount - Rs 1.99 Per Unit").type == INTEREST_PAYMENT


# ------------------------------------------------------------- guardrails
class TestNothingIsSilentlyDropped:
    def test_unknown_text_is_flagged(self):
        c = one("Mf")
        assert c.type == UNKNOWN
        assert not c.parsed_ok
        assert c.needs_review

    def test_empty_purpose_yields_no_components(self):
        assert parse_purpose("").components == []

    def test_every_component_is_either_parsed_or_reviewed(self):
        """The core invariant: no third state exists."""
        for text in [
            "Bonus 1:1", "Mf", "Demerger", "Annual General Meeting",
            "Rights 1:1 @ Premium Rs 5/-", "Dividend - Rs 4 Per Share",
            "Some Entirely Novel Corporate Event 2027",
        ]:
            for c in parse_purpose(text, 10).components:
                assert c.parsed_ok or c.needs_review, f"{text!r} -> {c.type} vanished"


# ------------------------------------------------- whole-census regression
class TestFullCensus:
    """Run the parser over all 8,543 real rows and pin the coverage.

    These thresholds are a regression guard: if a parser change pushes the
    review queue up, this fails and says by how much.
    """

    @pytest.fixture(scope="class")
    def rows(self):
        if not CENSUS.exists():
            pytest.skip("census fixture missing")
        return [r for r in json.loads(CENSUS.read_text(encoding="utf-8")) if isinstance(r, dict)]

    def test_census_is_the_expected_size(self, rows):
        assert len(rows) == 8543

    def test_no_row_raises(self, rows):
        for r in rows:
            parse_purpose(r.get("subject", ""), r.get("faceVal"))

    def test_review_rate_stays_low(self, rows):
        flagged = sum(
            1
            for r in rows
            if any(c.needs_review for c in parse_purpose(r.get("subject", ""), r.get("faceVal")).components)
        )
        rate = flagged / len(rows)
        assert rate < 0.02, f"review rate regressed to {rate:.2%} ({flagged} rows)"

    def test_high_severity_is_dominated_by_demergers(self, rows):
        """The irreducible residual: demergers genuinely need an external price."""
        high, demerger = 0, 0
        for r in rows:
            for c in parse_purpose(r.get("subject", ""), r.get("faceVal")).components:
                if c.needs_review and review_severity(c) == "high":
                    high += 1
                    if c.type in NEEDS_EXTERNAL:
                        demerger += 1
        assert high < 80, f"high-severity count regressed to {high}"
        assert demerger / high > 0.7, "unexpected non-external high-severity failures appeared"

    def test_every_structural_event_type_is_represented(self, rows):
        found = set()
        for r in rows:
            for c in parse_purpose(r.get("subject", ""), r.get("faceVal")).components:
                if c.parsed_ok:
                    found.add(c.type)
        for expected in (BONUS, SPLIT, RIGHTS, SPECIAL_DIVIDEND, RETURN_OF_CAPITAL,
                         DIVIDEND, INTERIM_DIVIDEND, AGM, BONUS_PREFERENCE):
            assert expected in found, f"{expected} never parsed from the real census"

    def test_no_ncrps_row_ever_becomes_an_equity_bonus(self, rows):
        """Belt and braces on the most dangerous failure mode."""
        for r in rows:
            subj = r.get("subject", "") or ""
            if "ncrps" not in subj.lower():
                continue
            for c in parse_purpose(subj, r.get("faceVal")).components:
                assert c.type != BONUS, f"{subj!r} misread as equity bonus"
                assert c.type not in PRICE_ADJUSTING
