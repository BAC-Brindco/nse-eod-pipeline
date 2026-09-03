"""Fetch bhavcopy, delivery and price-band files, choosing the right format.

Endpoint coverage measured against live NSE on 2026-08-31:

    UDiFF    /content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip
             200 from 2024-01-02 ; 404 for 2023
    legacy   /content/historical/EQUITIES/<YYYY>/<MON>/cm<DD><MON><YYYY>bhav.csv.zip
             200 up to ~2024-07 ; 404 from 2024-10
    delivery /products/content/sec_bhavdata_full_<DDMMYYYY>.csv
             200 from 2020 ; 404 for 2019
    bands    /content/equities/sec_list_<DDMMYYYY>.csv
"""

from __future__ import annotations

import datetime as dt
import hashlib

import polars as pl

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from ..parsers.bhav import (
    enrich,
    parse_bhavcopy,
    parse_delivery,
    parse_pricebands,
    unzip_single_csv,
)
from .http import ARCHIVES, NSENotFound, NSESession

log = get_logger(__name__)

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def udiff_url(d: dt.date) -> str:
    return f"{ARCHIVES}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"


def legacy_url(d: dt.date) -> str:
    mon = MONTHS[d.month - 1]
    return f"{ARCHIVES}/content/historical/EQUITIES/{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip"


def delivery_url(d: dt.date) -> str:
    return f"{ARCHIVES}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"


def priceband_url(d: dt.date) -> str:
    return f"{ARCHIVES}/content/equities/sec_list_{d:%d%m%Y}.csv"


def _candidate_urls(d: dt.date, s: Settings) -> list[tuple[str, str]]:
    """Ordered (label, url) candidates for a date.

    Both formats are always tried, newest-first for recent dates and
    legacy-first for old ones. Being wrong about the cutover costs one extra
    404, whereas hardcoding a single format loses the day entirely.
    """
    if d >= s.udiff_first_date:
        return [("udiff", udiff_url(d)), ("legacy", legacy_url(d))]
    return [("legacy", legacy_url(d)), ("udiff", udiff_url(d))]


class BhavcopyUnavailable(RuntimeError):
    """No bhavcopy exists for this date in any format (holiday, or out of range)."""


def fetch_bhavcopy(
    d: dt.date, session: NSESession, settings: Settings | None = None
) -> tuple[pl.DataFrame, str, str, str]:
    """Fetch and parse the bhavcopy for ``d``.

    Returns ``(frame, source_format, source_file, content_hash)``.
    """
    s = settings or get_settings()
    last_error: Exception | None = None
    for label, url in _candidate_urls(d, s):
        try:
            content = session.get_bytes(url)
        except NSENotFound:
            log.debug("bhavcopy_absent", date=str(d), format=label)
            continue
        except Exception as exc:  # network/retry exhaustion
            last_error = exc
            log.warning("bhavcopy_fetch_error", date=str(d), format=label, error=str(exc))
            continue

        name = url.rsplit("/", 1)[-1]
        session.archive(content, f"bhav/{d:%Y}/{name}")
        text = unzip_single_csv(content)
        df = parse_bhavcopy(text, expected_date=d)
        content_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        log.info("bhavcopy_parsed", date=str(d), format=label, rows=len(df))
        return df, label, name, content_hash

    if last_error is not None:
        raise BhavcopyUnavailable(f"bhavcopy fetch failed for {d}: {last_error}") from last_error
    raise BhavcopyUnavailable(f"no bhavcopy published for {d} (holiday or out of range)")


def fetch_delivery(
    d: dt.date, session: NSESession, settings: Settings | None = None
) -> pl.DataFrame | None:
    """Fetch delivery/VWAP data. Returns None when the file does not exist.

    Absence is expected before 2020 and must never fail the run: UDiFF carries
    the authoritative prices, and delivery columns are nullable by design.
    """
    s = settings or get_settings()
    if d < s.delivery_first_date:
        return None
    url = delivery_url(d)
    try:
        content = session.get_bytes(url)
    except NSENotFound:
        log.info("delivery_absent", date=str(d))
        return None
    except Exception as exc:
        log.warning("delivery_fetch_error", date=str(d), error=str(exc))
        return None
    session.archive(content, f"delivery/{d:%Y}/{url.rsplit('/', 1)[-1]}")
    try:
        return parse_delivery(content.decode("utf-8", errors="replace"))
    except Exception as exc:
        log.warning("delivery_parse_error", date=str(d), error=str(exc))
        return None


def fetch_pricebands(
    d: dt.date, session: NSESession, settings: Settings | None = None
) -> pl.DataFrame | None:
    """Fetch the circuit-band list. Optional; absence must not fail the run."""
    url = priceband_url(d)
    try:
        content = session.get_bytes(url)
    except NSENotFound:
        log.info("pricebands_absent", date=str(d))
        return None
    except Exception as exc:
        log.warning("pricebands_fetch_error", date=str(d), error=str(exc))
        return None
    session.archive(content, f"bands/{d:%Y}/{url.rsplit('/', 1)[-1]}")
    try:
        return parse_pricebands(content.decode("utf-8", errors="replace"))
    except Exception as exc:
        log.warning("pricebands_parse_error", date=str(d), error=str(exc))
        return None


def fetch_eod_frame(
    d: dt.date, session: NSESession, settings: Settings | None = None
) -> tuple[pl.DataFrame, str, str, str]:
    """Fetch and assemble the full enriched bronze frame for one date."""
    df, fmt, src_file, content_hash = fetch_bhavcopy(d, session, settings)
    delivery = fetch_delivery(d, session, settings)
    bands = fetch_pricebands(d, session, settings)
    enriched = enrich(df, delivery, bands)
    return enriched, fmt, src_file, content_hash
