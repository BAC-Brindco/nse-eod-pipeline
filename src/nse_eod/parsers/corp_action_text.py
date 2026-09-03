"""Parse NSE corporate-action `purpose` free text into structured events.

Designed against a real census, not guesswork: 8,543 rows scraped from the NSE
corporate-actions API covering 2023-01-01..2026-08-31, which reduced to 1,583
distinct subject strings and 325 templates. The census lives in
``tests/fixtures/subject_templates.txt`` and drives the test suite.

Three findings from that census shape this module:

1. **NSE prints the rights PREMIUM, not the subscription price.**
   ``Rights 1:1 @ Premium Rs 11.25/-`` on a stock with ``faceVal=5`` means an
   all-in subscription price of **16.25**, not 11.25. Ten of the 147 real rights
   rows read ``@ Premium Rs 0/-`` — an issue *at par*, S = face value. Treating
   the premium as S would yield ``(1 + N*0)/(1+N) = 1/(1+N)``: a fabricated
   50%+ crash. Hence ``face_value`` is mandatory to price a rights issue.

2. **The NCRPS trap.** ``Bonus Ncrps 4:1`` / ``Scheme Of Arrangement - Bonus
   Ncrps 1:10`` are bonuses of *non-convertible redeemable preference shares*.
   The equity share count is unchanged, so the equity price must NOT be
   rescaled. A naive ``Bonus (\\d+):(\\d+)`` regex reads ``4:1`` as a 4:1 equity
   bonus and destroys 80% of that symbol's price history. ``BONUS_PREFERENCE``
   is therefore matched *before* the bonus rule and carries factor 1.0.

3. **One purpose string can carry several events.** Components are joined by
   ``/``, ``&``, or nothing at all, and NSE sometimes drops the separator
   entirely (``Annual General Meetingdividend - Rs 6 Per Share``, 9 rows). So a
   single raw row fans out to N events, each with a ``component_ix``.

Nothing is ever silently dropped or guessed: every component either becomes a
typed event or a review-queue row (usually both, when a compound purpose parses
only partially).
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

PARSER_VERSION = "1.1.0"

# ---------------------------------------------------------------- event types
# price-adjusting
BONUS = "BONUS"
SPLIT = "SPLIT"
FACE_VALUE_CHANGE = "FACE_VALUE_CHANGE"
RIGHTS = "RIGHTS"
SPECIAL_DIVIDEND = "SPECIAL_DIVIDEND"
RETURN_OF_CAPITAL = "RETURN_OF_CAPITAL"
# total-return only
DIVIDEND = "DIVIDEND"
INTERIM_DIVIDEND = "INTERIM_DIVIDEND"
# recognised, no price effect
AGM = "AGM"
EGM = "EGM"
INTEREST_PAYMENT = "INTEREST_PAYMENT"
BUYBACK = "BUYBACK"
NO_ADJUST = "NO_ADJUST"
# recognised but needs external data
DEMERGER = "DEMERGER"
SPINOFF = "SPINOFF"
CAPITAL_REDUCTION = "CAPITAL_REDUCTION"
REDEMPTION = "REDEMPTION"
SCHEME_OF_ARRANGEMENT = "SCHEME_OF_ARRANGEMENT"
BONUS_PREFERENCE = "BONUS_PREFERENCE"
UNKNOWN = "UNKNOWN"

PRICE_ADJUSTING = frozenset(
    {BONUS, SPLIT, FACE_VALUE_CHANGE, RIGHTS, SPECIAL_DIVIDEND, RETURN_OF_CAPITAL}
)
DIVIDEND_TYPES = frozenset({DIVIDEND, INTERIM_DIVIDEND, SPECIAL_DIVIDEND})
NEEDS_EXTERNAL = frozenset({DEMERGER, SPINOFF, CAPITAL_REDUCTION, REDEMPTION})
NON_ADJUSTING = frozenset(
    {AGM, EGM, INTEREST_PAYMENT, BUYBACK, NO_ADJUST, BONUS_PREFERENCE, SCHEME_OF_ARRANGEMENT}
)


@dataclasses.dataclass
class ParsedComponent:
    """One atomic event extracted from a purpose string."""

    type: str
    component_ix: int
    raw_text: str
    ratio_num: Decimal | None = None
    ratio_den: Decimal | None = None
    premium: Decimal | None = None
    sub_price: Decimal | None = None
    face_value: Decimal | None = None
    div_amount: Decimal | None = None
    is_special: bool = False
    parsed_ok: bool = True
    confidence: str = "high"          # high | medium | low
    review_reason: str | None = None  # set => also emit a review-queue row

    @property
    def needs_review(self) -> bool:
        return self.review_reason is not None

    def event_uid(self, isin: str, ex_date) -> str:
        """Deterministic identity, so re-parsing the same announcement upserts."""
        norm = _normalize_for_uid(self.raw_text)
        key = f"{isin}|{ex_date}|{self.type}|{self.component_ix}|{norm}"
        return hashlib.md5(key.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class ParseResult:
    components: list[ParsedComponent]
    purpose: str
    unparsed_tail: list[str] = dataclasses.field(default_factory=list)

    @property
    def any_price_adjusting(self) -> bool:
        return any(c.type in PRICE_ADJUSTING for c in self.components)

    @property
    def all_parsed(self) -> bool:
        return all(c.parsed_ok for c in self.components) and not self.unparsed_tail


# ------------------------------------------------------------------ utilities
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Collapse whitespace and normalise the punctuation NSE varies freely."""
    t = text.replace(" ", " ")
    t = t.replace("–", "-").replace("—", "-")  # en/em dash
    t = t.replace("’", "'")
    return _WS.sub(" ", t).strip()


def _normalize_for_uid(text: str) -> str:
    return _WS.sub(" ", _clean(text).lower()).strip(" -.")


def _num(raw: str | None) -> Decimal | None:
    """Parse a numeric token. Handles ``09``, ``1,250``, ``.50``, ``2.``"""
    if raw is None:
        return None
    t = raw.strip().replace(",", "").rstrip("/-").strip()
    if not t:
        return None
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


# ``Rs``/``Re``/``Rs.``/``Re.``/``INR`` then an amount. NSE uses ``Re`` for
# sub-rupee amounts (~1,300 rows in the census) and sometimes omits the space.
_CUR = r"(?:Rs|Re|INR|₹)\.?\s*"
_AMT = r"(\d[\d,]*(?:\.\d+)?|\.\d+)"


# ---------------------------------------------------------------- splitting
# Components are separated by '/', '&', ' And ', or ','. We must NOT split on
# the '-' in "Dividend - Rs 4" nor inside "Sub-Division", and must not split a
# ratio like "1:5". Splitting on '/' is unsafe next to "Rs 4/-" so the trailing
# "/-" is protected first.
_PROTECT_SLASH_DASH = re.compile(r"/-")
_SLASH_TOKEN = "\x00SLASHDASH\x00"

# NOTE: comma is deliberately NOT a separator. NSE writes thousands separators
# ("Rs 1,250") and dates ("Dividend For Apr 01 To Apr 15, 2025 - Re 0.66"), and
# splitting on comma shreds both. '/', '&' and ' And ' cover every real
# separator in the 325-template census.
_SPLIT_RE = re.compile(r"\s*(?:/|&|\bAnd\b)\s*", re.IGNORECASE)

# NSE occasionally concatenates a meeting label straight onto the next component
# with no separator at all. Seen in the census as:
#   "Annual General Meetingdividend - Rs 6 Per Share"          (9 rows)
#   "Interim Dividend - Rs 8 Per Share Special Dividend - Rs 67 Per Share"
_GLUED_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(Annual General Meeting)(?=[A-Za-z])", re.IGNORECASE), r"\1/"),
    (re.compile(r"(Extra[\s-]?Ordinary General Meeting)(?=[A-Za-z])", re.IGNORECASE), r"\1/"),
    # a second dividend clause starting mid-string with no separator
    (
        re.compile(r"(Per\s+(?:Share|Sh|Unit))\s+(?=(?:Special|Interim|Final)\b)", re.IGNORECASE),
        r"\1/",
    ),
    (
        re.compile(r"(Per\s+(?:Share|Sh|Unit))\s+(?=Dividend\b)", re.IGNORECASE),
        r"\1/",
    ),
    # "Interimdividend - Re 1 Per Share": the qualifier is glued onto the label,
    # which defeats the \bdividend\b boundary. Re-insert the space.
    (
        re.compile(r"\b(Interim|Final|Special|Annual)(dividend)\b", re.IGNORECASE),
        r"\1 \2",
    ),
)


def split_components(purpose: str) -> list[str]:
    """Break a purpose string into candidate components."""
    t = _clean(purpose)
    for pat, rep in _GLUED_FIXES:
        t = pat.sub(rep, t)
    t = _PROTECT_SLASH_DASH.sub(_SLASH_TOKEN, t)
    # Strip stray leading/trailing separators BEFORE restoring "/-", otherwise
    # the strip eats the dash of a protected "Rs 0/-" and the premium is lost.
    parts = [p.strip(" -").replace(_SLASH_TOKEN, "/-") for p in _SPLIT_RE.split(t)]
    return [p for p in parts if p]


# --------------------------------------------------------------- rule matchers
# Order matters. NCRPS must be tested before the generic bonus rule.

# Ratios can be fractional in the real feed: "Rights 1:11.10", "Rights 1:19.07".
# Capturing only \d+ silently truncates 11.10 to 11 -- a 1% error in N that
# quietly biases every pre-ex price. Always allow a decimal part.
_RATIO = r"(\d+(?:\.\d+)?)"

_RE_NCRPS = re.compile(
    r"(?:bonus|issue|rights?)\s*(?:-\s*)?(?:of\s*)?"
    r"(?:\d+\s+)?"
    r"(?:ncrps|ncrp|n\.c\.r\.p\.s|ccps|ccd|preference\s+shares?|pref\.?\s+shares?)"
    r"(?:\s*" + _RATIO + r"\s*:\s*" + _RATIO + r")?",
    re.IGNORECASE,
)
_RE_NCRPS_LOOSE = re.compile(
    r"\bncrps?\b|\bccps\b|\bpreference\s+share", re.IGNORECASE
)

# The label and the ratio can be separated by the word "Issue" and/or arbitrary
# punctuation. Real census rows that a `\s*`-only gap silently missed:
#     "Bonus- 1:2"                            (AJANTPHARM 2022-06-22)
#     "Rights Issue 30:37@ Premium Rs 53/-"   (ASIANTILES 2022-04-11)
# Missing the AJANTPHARM bonus left that symbol's entire pre-2022 history
# unadjusted by a third.
_LABEL_GAP = r"(?:\s*(?:issue|of)\b)?[\s\-:,.]*"

_RE_BONUS = re.compile(
    r"\bbonus\b" + _LABEL_GAP + _RATIO + r"\s*:\s*" + _RATIO, re.IGNORECASE
)

# A bonus paid in DEBENTURES / NCDs, not equity shares. The share count does not
# change, but value IS distributed, so the correct factor needs the debenture's
# value -- which the text does not give. Real: BRITANNIA 2021-05-25
# "Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held".
_RE_BONUS_DEBENTURE = re.compile(
    r"\bbonus\b.{0,40}?\b(?:debenture|ncd|bond)s?\b", re.IGNORECASE | re.DOTALL
)

# "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"
# Also seen: "From Rs10/-" (no space), and "To Re 1/-".
_RE_SPLIT = re.compile(
    r"(?:face\s*value\s*)?(?:split|sub-?division|consolidation)"
    r"[^\d]*?from\s*" + _CUR + _AMT + r"[^\d]*?to\s*" + _CUR + _AMT,
    re.IGNORECASE,
)

# "Rights 1:1 @ Premium Rs 11.25/-", "Rights 2:5@ Premium Rs 2/-",
# "Rights 3:2 @ Prm Rs 0.63/-", "Rights 1:5" (no premium given)
_RE_RIGHTS = re.compile(
    r"\brights?\b" + _LABEL_GAP + _RATIO + r"\s*:\s*" + _RATIO
    # "Partly Paid" can sit between the ratio and the premium (ABFRL 2020-06-30).
    + r"(?:\s*partly\s+paid)?"
    # Currency prefix is OPTIONAL: the census contains "Rights 1:9 @ Premium 91".
    + r"(?:\s*@?\s*(?:at\s+)?(?:premium|prem|prm)(?:\s+of)?\s*(?:" + _CUR + r")?" + _AMT + r")?",
    re.IGNORECASE,
)
_RE_RIGHTS_WARRANT = re.compile(r"warrants?\b", re.IGNORECASE)
# Partly-paid rights: only the first call is paid at ex-date, so the effective
# subscription price is NOT the full premium. Refuse to compute.
_RE_RIGHTS_PARTLY_PAID = re.compile(r"partly\s+paid", re.IGNORECASE)

# Dividends. The amount appears on EITHER side of the label in the real feed:
#     "Dividend - Rs 4 Per Share"              (after)
#     "Rs 1.2975 Dividend Per Unit"            (before)
#     "Re 0.26 As Dividend"                    (before)
#     "Rs 1.08 Per Unit As Dividend"           (before)
# So detect the label + kind, then take the amount from anywhere in the clause
# (`split_components` has already reduced the text to one clause).
# Real misspellings and abbreviations from the census. These affect the
# total-return series (not the price series), so a miss silently understates TR:
#   "Divdend - Rs 0.50 Per Share"      "Interim Divdend - Rs 2 Per Share"
#   "Interim Dividned - Rs 135 Per Share"
#   "Div - Rs 0.50 Per Sh"             "Int Div-Rs 0.5 Per Sh"
_RE_DIV_LABEL = re.compile(
    r"\b(?:dividend|divdend|dividned|divdends?|divident|dividen)\b|\bdiv\b\.?",
    re.IGNORECASE,
)
_RE_DIV_KIND = re.compile(
    r"\b(?P<kind>special\s+interim|interim\s+special|special|interim|int|final|annual|exempt)\b"
    r"(?=.{0,20}?(?:dividend|divdend|dividned|divident|dividen|\bdiv\b))",
    re.IGNORECASE,
)

# --------------------------------------------------- REIT / InvIT components
# A "Distribution - Rs X Per Unit Consists Of ..." string splits into clauses,
# each naming one component. Because `split_components` has already reduced the
# text to a single clause, the amount ANYWHERE in that clause belongs to it --
# far more robust than trying to match the amount at a fixed offset from the
# label, which NSE writes in both orders:
#     "Return Of Capital - Re 0.3848 Per Unit"
#     "Rs 1.4534 Per Unit As Return Of Capital"
#     "Re 0.21 Per Unit In The Form Of Return Of Capital"
#
# Capital-returning components reduce unit NAV and therefore DO adjust price
# (Bloomberg treats return of capital exactly like a special cash dividend).
# Note the typos are real census entries: "Retun Of Capital", "Interst Amount",
# "Tresury Income", "Return On Captial".
_RE_CAPITAL_RETURN_LABEL = re.compile(
    r"(?:ret[ur]*n|repayment|repaid)\s+(?:of|on)\s+(?:capital|capt?ial|spv|debt)"
    # NOTE: "reduction" is deliberately absent. "Capital Reduction" is a distinct
    # corporate event that needs external data, not a REIT capital return, and it
    # is matched later by _RE_CAPRED. Including it here swallowed those rows and
    # mistyped them as RETURN_OF_CAPITAL.
    r"|capital\s+repayment"
    r"|repayment\s+of\s+(?:spv|debt|shareholder|capital|principal|loan)"
    r"|principal\s+(?:\w+\s+)?(?:payment|repayment)"
    r"|proceeds\s+of\s+amorti[sz]ation"
    r"|\bspv\s+(?:level\s+)?debt\b"
    r"|\bspv\s+loan\b"
    r"|amorti[sz]ation"
    r"|\bncds?\b",
    re.IGNORECASE,
)
# Income-type components: NOT price-adjusting, and not equity dividends either.
_RE_UNIT_INCOME_LABEL = re.compile(
    r"treasury\s+income|tresury\s+income|other\s+income|interst|interest", re.IGNORECASE
)
# Any currency-tagged amount in the clause, and a bare-number fallback for the
# rows where NSE omits the currency entirely ("Return Of Capital 0.19 Per Unit").
_RE_ANY_AMT = re.compile(_CUR + _AMT, re.IGNORECASE)
_RE_BARE_AMT = re.compile(r"(?<![\d.:])" + _AMT + r"(?![\d.]*\s*:)")


def _find_amount(clause: str) -> Decimal | None:
    """Extract the monetary amount from a single clause.

    Currency-tagged amounts win; a bare number is accepted only when no tagged
    amount exists, and never when it looks like half of a ratio.
    """
    m = _RE_ANY_AMT.search(clause)
    if m:
        return _num(m.group(1))
    if ":" in clause:  # ratio-bearing clause: refuse to read a leg as an amount
        return None
    m = _RE_BARE_AMT.search(clause)
    return _num(m.group(1)) if m else None

# Truncated/misspelled meeting labels are real census entries. Note "Meting"
# has a single 'e', so the stem must allow both "met" and "meet".
#   "Annual General Meetin"  "Extra-Ordinary General Meting"
_MEETING = r"me{1,2}t(?:ing|in|ng)?\b"
_RE_AGM = re.compile(r"annual\s+general\s+" + _MEETING + r"|\bagm\b", re.IGNORECASE)
_RE_EGM = re.compile(
    r"extra[\s-]?ordinary\s+general\s+" + _MEETING + r"|\begm\b|\beogm\b"
    r"|court\s+convened\s+meeting|postal\s+ballot|general\s+" + _MEETING,
    re.IGNORECASE,
)
# "Nterest Amount- Rs 3.0556" -- the leading 'i' is dropped in the source, so the
# literal word is "nterest". An optional leading 'i' covers both spellings.
_INTEREST_WORD = r"i?nterest|intrest|interst"
_RE_INTEREST = re.compile(
    r"(?:" + _INTEREST_WORD + r")\s+payment"
    r"|(?:" + _INTEREST_WORD + r")\s+amount"
    r"|\b(?:" + _INTEREST_WORD + r")\b"
    r"|coupon|other\s+income|treasury\s+income|tresury\s+income|income\s+tax\s+refund",
    re.IGNORECASE,
)
_RE_BUYBACK = re.compile(r"buy\s*-?\s*back", re.IGNORECASE)
_RE_DEMERGER = re.compile(r"\bdemerger\b|\bde-?merger\b", re.IGNORECASE)
_RE_SPINOFF = re.compile(r"spin[\s-]?off", re.IGNORECASE)
_RE_CAPRED = re.compile(r"capital\s+reduction|reduction\s+of\s+capital", re.IGNORECASE)
_RE_REDEMPTION = re.compile(r"\bredemption\b|\bredeem", re.IGNORECASE)
_RE_SCHEME = re.compile(r"scheme\s+of\s+(?:arrangement|amalgamation)|amalgamation", re.IGNORECASE)
_RE_MERGER = re.compile(r"\bmerger\b", re.IGNORECASE)

# Purely informational labels that legitimately carry no price effect.
_RE_INFO_ONLY = re.compile(
    r"^(?:board\s+meeting|results?|financial\s+results?|change\s+in\s+(?:name|symbol)"
    r"|listing|record\s+date|book\s+closure|no\s+dividend|nil|-|open\s+offer"
    r"|voluntary\s+delisting|delisting|suspension|split\s+of\s+shares?)\b",
    re.IGNORECASE,
)


def _classify(text: str, ix: int, face_value: Decimal | None) -> ParsedComponent:
    """Classify one component. Order of tests is load-bearing."""
    t = _clean(text)
    mk = lambda **kw: ParsedComponent(component_ix=ix, raw_text=t, **kw)  # noqa: E731

    # ---- 1. NCRPS / preference bonus. MUST precede the bonus rule. ----------
    m = _RE_NCRPS.search(t)
    if m:
        # Preference / convertible instruments: the EQUITY share count is
        # unchanged, so factor_price stays 1.0. Typed (not UNKNOWN) so it does
        # not clutter the review queue, and recorded so the reason is auditable.
        return mk(
            type=BONUS_PREFERENCE,
            ratio_num=_num(m.group(1)),
            ratio_den=_num(m.group(2)),
            parsed_ok=True,
            confidence="high",
        )
    if _RE_NCRPS_LOOSE.search(t):
        # Some phrasing we do not recognise, but it mentions preference shares
        # and a bonus. Refuse to guess: no price effect, flag for a human.
        return mk(
            type=BONUS_PREFERENCE,
            parsed_ok=False,
            confidence="low",
            review_reason="ambiguous_ratio",
        )

    # ---- 2. Face-value split / sub-division ---------------------------------
    m = _RE_SPLIT.search(t)
    if m:
        fv_old, fv_new = _num(m.group(1)), _num(m.group(2))
        if fv_old and fv_new and fv_old > 0 and fv_new > 0:
            if fv_new == fv_old:
                return mk(
                    type=SPLIT,
                    ratio_num=fv_old,
                    ratio_den=fv_new,
                    parsed_ok=False,
                    confidence="low",
                    review_reason="ambiguous_ratio",
                )
            return mk(type=SPLIT, ratio_num=fv_old, ratio_den=fv_new, confidence="high")
        return mk(
            type=SPLIT, parsed_ok=False, confidence="low", review_reason="ambiguous_ratio"
        )

    # ---- 3. Equity bonus ----------------------------------------------------
    # A bonus paid in debentures/NCDs distributes value but leaves the share
    # count unchanged; the factor needs the instrument's value, which the text
    # does not supply. Checked before the equity-bonus rule.
    if _RE_BONUS_DEBENTURE.search(t):
        return mk(
            type=SCHEME_OF_ARRANGEMENT,
            parsed_ok=False,
            confidence="low",
            review_reason="needs_external_price",
        )

    m = _RE_BONUS.search(t)
    if m:
        num, den = _num(m.group(1)), _num(m.group(2))
        if num and den and num > 0 and den > 0:
            return mk(type=BONUS, ratio_num=num, ratio_den=den, confidence="high")
        return mk(
            type=BONUS, parsed_ok=False, confidence="low", review_reason="ambiguous_ratio"
        )
    if re.search(r"\bbonus\b", t, re.I) and not _RE_NCRPS_LOOSE.search(t):
        return mk(
            type=BONUS, parsed_ok=False, confidence="low", review_reason="ambiguous_ratio"
        )

    # ---- 4. Rights ----------------------------------------------------------
    m = _RE_RIGHTS.search(t)
    if m:
        num, den, prem = _num(m.group(1)), _num(m.group(2)), _num(m.group(3))
        if _RE_RIGHTS_PARTLY_PAID.search(t):
            # Only the first call is paid at ex-date, so the printed premium
            # overstates S. Real: ABFRL 2020-06-30 "Rights 9:77 Partly Paid".
            return mk(
                type=RIGHTS,
                ratio_num=num,
                ratio_den=den,
                premium=prem,
                parsed_ok=False,
                confidence="low",
                review_reason="unparsed_pattern",
            )
        if _RE_RIGHTS_WARRANT.search(t):
            # "Rights 1:50 @ Premium Rs 690/- With 17 Warrants For 50 Equity Shares"
            # The warrant leg changes the economics and NSE does not give us its
            # terms in a parseable form. Do not guess.
            return mk(
                type=RIGHTS,
                ratio_num=num,
                ratio_den=den,
                premium=prem,
                parsed_ok=False,
                confidence="low",
                review_reason="unparsed_pattern",
            )
        if not (num and den and den > 0):
            return mk(
                type=RIGHTS, parsed_ok=False, confidence="low", review_reason="ambiguous_ratio"
            )
        if prem is None:
            # No premium printed: cannot form S. Face value alone is a guess.
            return mk(
                type=RIGHTS,
                ratio_num=num,
                ratio_den=den,
                parsed_ok=False,
                confidence="low",
                review_reason="ambiguous_ratio",
            )
        if face_value is None or face_value <= 0:
            return mk(
                type=RIGHTS,
                ratio_num=num,
                ratio_den=den,
                premium=prem,
                parsed_ok=False,
                confidence="low",
                review_reason="ambiguous_ratio",
            )
        # THE key insight: NSE prints the premium over face value, so the
        # all-in subscription price is face_value + premium.
        return mk(
            type=RIGHTS,
            ratio_num=num,
            ratio_den=den,
            premium=prem,
            sub_price=face_value + prem,
            face_value=face_value,
            confidence="high",
        )

    # ---- 4b. Capital reduction / redemption, BEFORE the capital-return rule --
    # "Capital Reduction" is a distinct event needing external data; it must not
    # be absorbed by the REIT return-of-capital matcher below.
    if _RE_CAPRED.search(t):
        return mk(type=CAPITAL_REDUCTION, parsed_ok=False, confidence="low",
                  review_reason="needs_external_price")
    if _RE_REDEMPTION.search(t):
        return mk(type=REDEMPTION, parsed_ok=False, confidence="low",
                  review_reason="needs_external_price")

    # ---- 5. REIT / InvIT capital return, before the dividend rule -----------
    # Must precede dividends: "Distribution ... Return Of Capital Re 0.36 Per
    # Unit" would otherwise be swallowed by the dividend rule if the clause also
    # happens to mention a dividend.
    if _RE_CAPITAL_RETURN_LABEL.search(t):
        amt = _find_amount(t)
        if amt is not None and amt > 0:
            # Confidence is medium, not high: these are unit trusts with ~120
            # near-unique phrasings, and they are excluded from the equity
            # universe anyway, so a mis-read cannot contaminate the EQ panel.
            return mk(
                type=RETURN_OF_CAPITAL,
                div_amount=amt,
                is_special=True,
                confidence="medium",
            )
        return mk(
            type=RETURN_OF_CAPITAL,
            parsed_ok=False,
            confidence="low",
            review_reason="unparsed_pattern",
        )

    # Unit income (interest / treasury / other income): no price effect.
    if _RE_UNIT_INCOME_LABEL.search(t) and re.search(r"per\s+unit", t, re.I):
        return mk(type=INTEREST_PAYMENT, confidence="high")

    # ---- 6. Dividends -------------------------------------------------------
    if _RE_DIV_LABEL.search(t):
        km = _RE_DIV_KIND.search(t)
        kind = (km.group("kind") or "").lower().strip() if km else ""
        amt = _find_amount(t)
        if amt is None or amt <= 0:
            # "Dividend" with no amount does occur. Not price-affecting, so this
            # only costs us the total-return leg -> low severity, not high.
            return mk(
                type=DIVIDEND, parsed_ok=False, confidence="low", review_reason="ambiguous_ratio"
            )
        if "special" in kind:
            # A special dividend DOES adjust price, so it must be typed as such
            # even when it appears mid-string in a compound purpose.
            return mk(type=SPECIAL_DIVIDEND, div_amount=amt, is_special=True, confidence="high")
        if "interim" in kind:
            return mk(type=INTERIM_DIVIDEND, div_amount=amt, confidence="high")
        return mk(type=DIVIDEND, div_amount=amt, confidence="high")

    # ---- 7. Needs external data -> typed, factor 1.0, high-severity review --
    if _RE_SPINOFF.search(t):
        return mk(type=SPINOFF, parsed_ok=False, confidence="low",
                  review_reason="needs_external_price")
    if _RE_DEMERGER.search(t):
        return mk(type=DEMERGER, parsed_ok=False, confidence="low",
                  review_reason="needs_external_price")
    if _RE_CAPRED.search(t):
        return mk(type=CAPITAL_REDUCTION, parsed_ok=False, confidence="low",
                  review_reason="needs_external_price")
    if _RE_REDEMPTION.search(t):
        return mk(type=REDEMPTION, parsed_ok=False, confidence="low",
                  review_reason="needs_external_price")
    if _RE_SCHEME.search(t) or _RE_MERGER.search(t):
        return mk(type=SCHEME_OF_ARRANGEMENT, parsed_ok=False, confidence="low",
                  review_reason="needs_external_price")

    # ---- 8. Recognised, deliberately no price effect ------------------------
    # These are high-volume (AGM alone is 1,550 of 8,543 census rows). Typing
    # them explicitly is what keeps the review queue small enough to be worked.
    if _RE_BUYBACK.search(t):
        return mk(type=BUYBACK, confidence="high")
    if _RE_AGM.search(t):
        return mk(type=AGM, confidence="high")
    if _RE_EGM.search(t):
        return mk(type=EGM, confidence="high")
    if _RE_INTEREST.search(t):
        return mk(type=INTEREST_PAYMENT, confidence="high")
    if _RE_INFO_ONLY.search(t):
        return mk(type=NO_ADJUST, confidence="high")

    # ---- 9. Genuinely unknown ----------------------------------------------
    return mk(
        type=UNKNOWN, parsed_ok=False, confidence="low", review_reason="unparsed_pattern"
    )


def parse_purpose(purpose: str, face_value: Decimal | float | int | str | None = None) -> ParseResult:
    """Parse a purpose string into one or more components.

    ``face_value`` comes from the API's ``faceVal`` field and is REQUIRED to
    price a rights issue (S = face_value + premium). Without it a rights
    component is flagged for review rather than guessed.
    """
    fv = _num(str(face_value)) if face_value is not None else None
    text = _clean(purpose or "")
    if not text:
        return ParseResult(components=[], purpose=purpose or "", unparsed_tail=[])

    parts = split_components(text)
    components: list[ParsedComponent] = []
    for ix, part in enumerate(parts):
        components.append(_classify(part, ix, fv))

    # A "Distribution - Rs X Per Unit Consists Of ..." headline creates a
    # leading component that duplicates the sum of the following ones. If we
    # already extracted labelled sub-components, drop a bare INTEREST/UNKNOWN
    # headline so the amounts are not double counted.
    components = _dedupe_distribution_headline(text, components)

    return ParseResult(components=components, purpose=purpose)


_RE_DISTRIBUTION_HEADLINE = re.compile(
    r"^distribution\b.*?\b(?:consist|comprising|comprises|consisting|consists)\b", re.IGNORECASE
)


def _dedupe_distribution_headline(
    full_text: str, components: list[ParsedComponent]
) -> list[ParsedComponent]:
    """Drop the headline total of a REIT distribution when its parts were parsed.

    ``Distribution - Rs 1.6876 Per Unit Consists Of Dividend Re 0.2958.../
    Interest - Re 0.7046.../Return Of Capital - Re 0.36...`` — the leading
    1.6876 is the SUM, not an additional payment. Counting both would
    double-adjust the price.
    """
    if not _RE_DISTRIBUTION_HEADLINE.search(_clean(full_text)):
        return components
    if len(components) < 2:
        return components
    head, *rest = components
    if not any(c.parsed_ok and c.div_amount for c in rest):
        return components
    # The headline is the SUM of the parts, not an extra payment. Neutralise it
    # so the amount is not counted twice.
    head.type = NO_ADJUST
    head.div_amount = None
    head.is_special = False
    head.parsed_ok = True
    head.confidence = "high"
    head.review_reason = None
    return [head, *rest]


def review_severity(component: ParsedComponent) -> str:
    """High severity means: price-affecting and unresolved.

    Severity is driven by the REASON as well as the type. A bonus paid in
    debentures is typed ``SCHEME_OF_ARRANGEMENT`` (which is not itself
    price-adjusting) yet genuinely distributes value and cannot be computed
    without the instrument's price -- so ``needs_external_price`` is high
    regardless of type. Keying only on type filed Britannia's 2021 debenture
    bonus as ``low`` and would have buried it.
    """
    if component.review_reason == "needs_external_price":
        return "high"
    if component.type in PRICE_ADJUSTING and not component.parsed_ok:
        return "high"
    if component.type in NEEDS_EXTERNAL:
        return "high"
    if component.type == UNKNOWN:
        return "medium"
    return "low"


def iter_price_adjusting(result: ParseResult) -> Iterable[ParsedComponent]:
    for c in result.components:
        if c.type in PRICE_ADJUSTING and c.parsed_ok:
            yield c
