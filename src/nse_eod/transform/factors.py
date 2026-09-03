"""Corporate-action factor formulas. Pure functions, no I/O, no DB.

Every factor multiplies bars **strictly before** ex_date. ``P`` is the close on
the last trading day before ex_date. Volume moves inversely to price for
share-count events, and is unaffected by cash events.

Formulas follow the Bloomberg DPDF conventions supplied in the spec.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal, InvalidOperation
from typing import Iterable

from ..parsers.corp_action_text import (
    BONUS,
    BONUS_PREFERENCE,
    FACE_VALUE_CHANGE,
    NEEDS_EXTERNAL,
    NON_ADJUSTING,
    PRICE_ADJUSTING,
    RETURN_OF_CAPITAL,
    RIGHTS,
    SPECIAL_DIVIDEND,
    SPLIT,
)

ONE = Decimal(1)

# factor_state values
COMPUTED = "computed"
NOT_APPLICABLE = "not_applicable"
SKIPPED_S_GT_P = "skipped_s_gt_p"
NEEDS_ANCHOR = "needs_anchor"
UNRESOLVED = "unresolved"

# Sanity envelope. A legitimate factor is a fraction (dilution) or, for a reverse
# split, greater than 1. Anything outside this is a parse error, not a real event.
MIN_FACTOR = Decimal("0.000001")
MAX_FACTOR = Decimal("1000")


class FactorError(ValueError):
    """The inputs cannot produce a defensible factor."""


@dataclasses.dataclass(frozen=True)
class Factor:
    """The result of evaluating one event."""

    factor_price: Decimal
    factor_vol: Decimal
    state: str
    note: str | None = None

    @property
    def adjusts_price(self) -> bool:
        return self.factor_price != ONE


def _d(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FactorError(f"not a number: {x!r}") from exc


def neutral(state: str = NOT_APPLICABLE, note: str | None = None) -> Factor:
    """A factor that changes nothing."""
    return Factor(factor_price=ONE, factor_vol=ONE, state=state, note=note)


def _check(f: Decimal, what: str) -> Decimal:
    if not (MIN_FACTOR <= f <= MAX_FACTOR):
        raise FactorError(f"{what} factor {f} outside plausible range")
    return f


# ---------------------------------------------------------------------------
# Share-count events: bonus, split, face-value change.
#
# Bonus and split reduce to the SAME primitive -- shares_before / shares_after --
# so both call this one function. Keeping it single-sourced means a fix to the
# rounding or validation applies to both, and the two can never drift apart.
# ---------------------------------------------------------------------------
def share_count_factor(shares_before: Decimal | float | int, shares_after: Decimal | float | int) -> Factor:
    """factor_price = before/after ; factor_vol = after/before."""
    b, a = _d(shares_before), _d(shares_after)
    if b <= 0 or a <= 0:
        raise FactorError(f"share counts must be positive, got before={b} after={a}")
    fp = _check(b / a, "share_count price")
    fv = _check(a / b, "share_count volume")
    return Factor(factor_price=fp, factor_vol=fv, state=COMPUTED)


def bonus_factor(ratio_num: Decimal | float | int, ratio_den: Decimal | float | int) -> Factor:
    """NSE 'Bonus N:D' = N new shares for every D held.

    ``Bonus 1:1`` -> before 1, after 2 -> factor_price 0.5, factor_vol 2.
    ``Bonus 13:10`` -> before 10, after 23 -> 10/23.
    """
    num, den = _d(ratio_num), _d(ratio_den)
    if num <= 0 or den <= 0:
        raise FactorError(f"bonus ratio must be positive, got {num}:{den}")
    return share_count_factor(den, den + num)


def split_factor(fv_old: Decimal | float | int, fv_new: Decimal | float | int) -> Factor:
    """Face-value split 'From Rs FV_old To Rs FV_new'.

    One old share of face value FV_old becomes FV_old/FV_new new shares, so
    shares_before = fv_new, shares_after = fv_old (in the same units), giving
    factor_price = fv_new / fv_old. ``From Rs 10 To Rs 2`` -> 0.2.
    A consolidation (reverse split, FV_new > FV_old) correctly yields > 1.
    """
    old, new = _d(fv_old), _d(fv_new)
    if old <= 0 or new <= 0:
        raise FactorError(f"face values must be positive, got {old} -> {new}")
    return share_count_factor(new, old)


# ---------------------------------------------------------------------------
# Cash events
# ---------------------------------------------------------------------------
def cash_factor(anchor_price: Decimal | float | int, cash: Decimal | float | int) -> Factor:
    """Special cash dividend / return of capital: (P - C) / P. Volume unaffected."""
    p, c = _d(anchor_price), _d(cash)
    if p <= 0:
        raise FactorError(f"anchor price must be positive, got {p}")
    if c < 0:
        raise FactorError(f"cash amount must be non-negative, got {c}")
    if c >= p:
        # A distribution at or above the whole share price would drive the
        # adjusted series to zero or negative. Real data error; refuse.
        raise FactorError(f"cash {c} >= anchor price {p}")
    return Factor(factor_price=_check((p - c) / p, "cash"), factor_vol=ONE, state=COMPUTED)


def rights_factor(
    ratio_num: Decimal | float | int,
    ratio_den: Decimal | float | int,
    sub_price: Decimal | float | int,
    anchor_price: Decimal | float | int,
) -> Factor:
    """Rights issue: (1 + N*(S/P)) / (1 + N), N = new/existing.

    Per Bloomberg DPDF, **not calculated when S > P** -- a subscription price
    above the market price means the rights are worthless and nobody
    subscribes, so there is no dilution to adjust for. Returns a neutral
    factor with state ``skipped_s_gt_p`` rather than raising, because this is a
    legitimate outcome, not an error.

    Note ``sub_price`` must be the ALL-IN price. NSE prints only the premium
    over face value, so callers pass ``face_value + premium``.
    """
    num, den = _d(ratio_num), _d(ratio_den)
    s, p = _d(sub_price), _d(anchor_price)
    if den <= 0 or num <= 0:
        raise FactorError(f"rights ratio must be positive, got {num}:{den}")
    if p <= 0:
        raise FactorError(f"anchor price must be positive, got {p}")
    if s < 0:
        raise FactorError(f"subscription price must be non-negative, got {s}")
    if s > p:
        return neutral(state=SKIPPED_S_GT_P, note=f"S={s} > P={p}; rights not exercised")
    n = num / den
    fp = (ONE + n * (s / p)) / (ONE + n)
    return Factor(factor_price=_check(fp, "rights"), factor_vol=ONE, state=COMPUTED)


def spinoff_factor(
    anchor_price: Decimal | float | int,
    spun_price: Decimal | float | int,
    ratio: Decimal | float | int,
) -> Factor:
    """Spin-off: 1 - (B*N)/P.

    Implemented for completeness and for use via ``corp_action_override``, but
    NOT reachable from the NSE EOD feed: no NSE end-of-day product publishes the
    spun-off entity's close (B). All 47 demergers in the census therefore land
    in the review queue with factor 1.0 rather than a guess.
    """
    p, b, n = _d(anchor_price), _d(spun_price), _d(ratio)
    if p <= 0:
        raise FactorError(f"anchor price must be positive, got {p}")
    if b < 0 or n < 0:
        raise FactorError("spun-off price and ratio must be non-negative")
    value = b * n
    if value >= p:
        raise FactorError(f"spun-off value {value} >= parent price {p}")
    return Factor(factor_price=_check(ONE - value / p, "spinoff"), factor_vol=ONE, state=COMPUTED)


def multi_spinoff_factor(
    anchor_price: Decimal | float | int,
    legs: Iterable[tuple[Decimal | float | int, Decimal | float | int]],
) -> Factor:
    """Two or more spin-offs: 1 - (V/P) where V = sum(B_i * N_i)."""
    p = _d(anchor_price)
    if p <= 0:
        raise FactorError(f"anchor price must be positive, got {p}")
    v = sum((_d(b) * _d(n) for b, n in legs), Decimal(0))
    if v >= p:
        raise FactorError(f"total spun-off value {v} >= parent price {p}")
    return Factor(factor_price=_check(ONE - v / p, "multi_spinoff"), factor_vol=ONE, state=COMPUTED)


def abnormal_cash_factor(
    anchor_price: Decimal | float | int,
    regular_cash: Decimal | float | int,
    special_cash: Decimal | float | int,
) -> Factor:
    """Bloomberg abnormal cash: (P - RC - SP) / (P - RC).

    Used when a special dividend shares its ex-date with an ordinary one. The
    ordinary component is removed from the base *and* the numerator, so only the
    abnormal part drives the adjustment -- which is what keeps an ordinary
    dividend out of the price series while still pricing the special correctly.
    """
    p, rc, sp = _d(anchor_price), _d(regular_cash), _d(special_cash)
    if p <= 0:
        raise FactorError(f"anchor price must be positive, got {p}")
    if rc < 0 or sp < 0:
        raise FactorError("cash amounts must be non-negative")
    base = p - rc
    if base <= 0:
        raise FactorError(f"P - RC = {base} is not positive")
    if sp >= base:
        raise FactorError(f"special cash {sp} >= P - RC = {base}")
    return Factor(
        factor_price=_check((base - sp) / base, "abnormal_cash"), factor_vol=ONE, state=COMPUTED
    )


def total_return_factor(anchor_price: Decimal | float | int, cash: Decimal | float | int) -> Factor:
    """Total-return leg: reinvest ANY dividend, ordinary included, at ex-date.

    Same shape as ``cash_factor``; separate function because the semantics
    differ -- this one is applied to *every* dividend, which is exactly what
    must NOT happen to the price series.
    """
    p, c = _d(anchor_price), _d(cash)
    if p <= 0:
        raise FactorError(f"anchor price must be positive, got {p}")
    if c < 0:
        raise FactorError(f"cash must be non-negative, got {c}")
    if c >= p:
        raise FactorError(f"cash {c} >= anchor price {p}")
    return Factor(factor_price=_check((p - c) / p, "total_return"), factor_vol=ONE, state=COMPUTED)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def compute_factor(
    event_type: str,
    *,
    ratio_num=None,
    ratio_den=None,
    sub_price=None,
    div_amount=None,
    anchor_price=None,
) -> Factor:
    """Evaluate one event into a price/volume factor pair.

    Returns a neutral factor (never raises) for event types that legitimately
    have no price effect, so callers do not need a type whitelist of their own.
    Raises ``FactorError`` only when a price-adjusting event has unusable inputs.
    """
    t = event_type

    if t in NON_ADJUSTING or t == BONUS_PREFERENCE:
        return neutral(NOT_APPLICABLE, note=f"{t}: no price effect by definition")

    if t in NEEDS_EXTERNAL:
        return neutral(UNRESOLVED, note=f"{t}: requires external price data")

    if t not in PRICE_ADJUSTING:
        # DIVIDEND / INTERIM_DIVIDEND / UNKNOWN: no price adjustment.
        return neutral(NOT_APPLICABLE, note=f"{t}: price series untouched")

    if t == BONUS:
        return bonus_factor(ratio_num, ratio_den)

    if t in (SPLIT, FACE_VALUE_CHANGE):
        return split_factor(ratio_num, ratio_den)

    if t == RIGHTS:
        if anchor_price is None:
            return neutral(NEEDS_ANCHOR, note="rights needs P")
        return rights_factor(ratio_num, ratio_den, sub_price, anchor_price)

    if t in (SPECIAL_DIVIDEND, RETURN_OF_CAPITAL):
        if anchor_price is None:
            return neutral(NEEDS_ANCHOR, note=f"{t} needs P")
        return cash_factor(anchor_price, div_amount)

    raise FactorError(f"unhandled price-adjusting type {t}")
