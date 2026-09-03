"""NSE trading calendar: holiday master + weekend rule + observed-bhavcopy truth.

    GET https://www.nseindia.com/api/holiday-master?type=trading

Returns a dict keyed by segment; ``CM`` is the cash market. Verified live
2026-08-31 (segments: CBM, CD, CM, CMOT, COM, EGR, FO, IRD, MF, NDM, NTRP, SLBS).

Three sources of truth, in increasing authority:
  1. ``weekend_rule``      -- Saturday/Sunday, always non-trading.
  2. ``nse_holiday_master``-- the published holiday list.
  3. ``observed_bhavcopy`` -- we actually downloaded a bhavcopy for that date.

(3) outranks (2) because NSE occasionally holds special sessions (Muhurat
trading, budget-day sessions) that are absent from, or mislabelled in, the
holiday master. A calendar that says "closed" on a day that really traded would
make the pipeline skip a real day forever.
"""

from __future__ import annotations

import datetime as dt

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from .http import WWW, NSESession

log = get_logger(__name__)

HOLIDAY_URL = f"{WWW}/api/holiday-master"
HOLIDAY_REFERER = f"{WWW}/resources/exchange-communication-holidays"
SEGMENT = "CM"

SRC_WEEKEND = "weekend_rule"
SRC_MASTER = "nse_holiday_master"
SRC_OBSERVED = "observed_bhavcopy"


def is_weekend(d: dt.date) -> bool:
    return d.weekday() >= 5


def never_trades(d: dt.date) -> bool:
    """NOTHING is assumed. Always False.

    This function used to return True for Sunday, on the reasoning that NSE has
    never traded on one. That was falsified by the data: **2026-02-01 was a
    Sunday and NSE published a full 180 KB bhavcopy** -- the Union Budget was
    presented that day and the exchange held a special session. The missing bar
    surfaced as a BIOFILCHEM +44% "unexplained jump", because its 2026-02-02
    prev_close (35.11) referred to a Sunday close we did not have.

    Confirmed Saturday sessions: 2020-02-01, 2020-11-14 (Diwali Muhurat),
    2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01.

    Every calendar assumption in this pipeline has eventually been wrong, and
    each one cost a real trading day. The bhavcopy's existence is the ONLY
    authority; a 404 costs ~0.16 s and settles it. Keeping the function (rather
    than deleting it) documents the lesson at the call site.
    """
    return False


def fetch_holidays(
    session: NSESession, segment: str = SEGMENT, settings: Settings | None = None
) -> list[dict]:
    """Fetch published holidays for a segment. Returns [] on failure.

    Deliberately non-fatal: the weekend rule plus observed bhavcopies keep the
    calendar usable even if this endpoint is unavailable.
    """
    try:
        data = session.get_json(HOLIDAY_URL, referer=HOLIDAY_REFERER, params={"type": "trading"})
    except Exception as exc:
        log.warning("holiday_fetch_failed", error=str(exc))
        return []

    if not isinstance(data, dict):
        log.warning("holiday_unexpected_payload", got=type(data).__name__)
        return []

    entries = data.get(segment) or []
    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        raw = (e.get("tradingDate") or "").strip()
        d = None
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
            try:
                d = dt.datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            log.debug("holiday_unparsed_date", value=raw)
            continue
        out.append(
            {
                "cal_date": d,
                "is_trading_day": False,
                "reason": (e.get("description") or "holiday").strip(),
                "segment": segment,
                "source": SRC_MASTER,
            }
        )
    log.info("holidays_fetched", segment=segment, count=len(out))
    return out


def weekend_rows(start: dt.date, end: dt.date, segment: str = SEGMENT) -> list[dict]:
    """Every weekend day in the range, marked non-trading."""
    rows = []
    d = start
    while d <= end:
        if is_weekend(d):
            rows.append(
                {
                    "cal_date": d,
                    "is_trading_day": False,
                    "reason": "weekend",
                    "segment": segment,
                    "source": SRC_WEEKEND,
                }
            )
        d += dt.timedelta(days=1)
    return rows


def weekday_rows(start: dt.date, end: dt.date, segment: str = SEGMENT) -> list[dict]:
    """Every weekday in the range, provisionally marked trading.

    Inserted with the lowest authority so a holiday-master row overwrites it.
    """
    rows = []
    d = start
    while d <= end:
        if not is_weekend(d):
            rows.append(
                {
                    "cal_date": d,
                    "is_trading_day": True,
                    "reason": None,
                    "segment": segment,
                    "source": SRC_WEEKEND,
                }
            )
        d += dt.timedelta(days=1)
    return rows


def observed_row(d: dt.date, segment: str = SEGMENT) -> dict:
    """The authoritative row: a bhavcopy for this date was successfully parsed."""
    return {
        "cal_date": d,
        "is_trading_day": True,
        "reason": "bhavcopy published",
        "segment": segment,
        "source": SRC_OBSERVED,
    }
