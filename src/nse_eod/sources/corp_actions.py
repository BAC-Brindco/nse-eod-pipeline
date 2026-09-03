"""Fetch corporate actions from the NSE API.

    GET https://www.nseindia.com/api/corporates-corporateActions
        ?index={equities|sme}&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY

The ``index`` parameter SEGMENTS the feed, and ``equities`` does not include the
SME platform. Fetching only ``equities`` left SME corporate actions entirely
missing, which surfaced as unexplained -80% to -96% single-day drops in the
adjusted panel for SM/ST names (ISHAN, GOLDSTAR, CELLECOR, COOLCAPS, VMARCIND,
JSLL, MOS, SECL). Verified: ``index=sme`` for Jan-Feb 2024 returns 6 rows that
``index=equities`` does not.

Verified live 2026-08-31: needs a warmed cookie jar, and the response window is
capped, so requests are chunked into ~60-day windows (the largest that returned
reliably; 1,039 rows was the biggest single response observed).

The date format is DD-MM-YYYY. Sending MM-DD-YYYY returns HTTP 200 with a
nonsense body rather than an error, so the response shape is validated before
use -- a silent wrong-format request would otherwise look like "no actions".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Iterator

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from .http import WWW, NSESession

log = get_logger(__name__)

CA_URL = f"{WWW}/api/corporates-corporateActions"
CA_REFERER = f"{WWW}/companies-listing/corporate-filings-actions"

# API field -> our column
FIELD_MAP = {
    "symbol": "symbol",
    "isin": "isin",
    "comp": "company",
    "series": "series",
    "faceVal": "face_value",
    "exDate": "ex_date",
    "recDate": "record_date",
    "bcStartDate": "bc_start_date",
    "bcEndDate": "bc_end_date",
    "ndStartDate": "nd_start_date",
    "ndEndDate": "nd_end_date",
    "caBroadcastDate": "ca_broadcast_date",
    "subject": "purpose_text",
}

_REQUIRED_FIELDS = {"symbol", "subject", "exDate"}


class CorpActionFetchError(RuntimeError):
    pass


def _parse_api_date(v) -> dt.date | None:
    """NSE writes dates as ``31-Aug-2026``, or ``-`` / null for absent."""
    if not v or not isinstance(v, str):
        return None
    v = v.strip()
    if v in ("-", "", "NA", "N/A"):
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    log.debug("ca_unparsed_date", value=v)
    return None


def _face_value(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def windows(start: dt.date, end: dt.date, days: int) -> Iterator[tuple[dt.date, dt.date]]:
    cur = start
    while cur <= end:
        nxt = min(cur + dt.timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + dt.timedelta(days=1)


def payload_hash(isin: str | None, ex_date, purpose: str, record_date) -> str:
    """Identity of an announcement, so daily re-scrapes dedupe.

    ISIN can be null in the feed, so the symbol-free key falls back to the
    purpose text, which is what actually distinguishes two announcements sharing
    an ISIN and ex-date (the compound-purpose case).
    """
    key = f"{(isin or '').upper()}|{ex_date or ''}|{(purpose or '').strip().lower()}|{record_date or ''}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def fetch_corp_actions(
    start: dt.date,
    end: dt.date,
    session: NSESession,
    settings: Settings | None = None,
) -> list[dict]:
    """Fetch raw corporate-action rows for a date range, chunked by window."""
    s = settings or get_settings()
    rows: list[dict] = []
    seen: set[str] = set()

    # The API is segmented by `index`, and `equities` EXCLUDES the SME platform.
    # Both segments must be fetched or SME splits/bonuses are simply absent.
    for index in s.ca_indices:
        idx_rows = 0
        for a, b in windows(start, end, s.ca_window_days):
            params = {
                "index": index,
                "from_date": a.strftime("%d-%m-%Y"),
                "to_date": b.strftime("%d-%m-%Y"),
            }
            try:
                data = session.get_json(CA_URL, referer=CA_REFERER, params=params)
            except Exception as exc:
                raise CorpActionFetchError(
                    f"corp actions [{index}] {a}..{b} failed: {exc}"
                ) from exc

            # The API answers 200 with a non-list body for a malformed request.
            # Treat that as an error rather than as "no corporate actions".
            if not isinstance(data, list):
                raise CorpActionFetchError(
                    f"corp actions [{index}] {a}..{b} returned {type(data).__name__}, "
                    f"expected list: {str(data)[:200]}"
                )
            if data and not isinstance(data[0], dict):
                raise CorpActionFetchError(
                    f"corp actions [{index}] {a}..{b} returned a list of "
                    f"{type(data[0]).__name__}, expected dicts: {str(data[:2])[:200]}"
                )
            for row in data:
                if not isinstance(row, dict):
                    continue
                if not _REQUIRED_FIELDS & set(row.keys()):
                    continue
                ph = payload_hash(
                    row.get("isin"), row.get("exDate"), row.get("subject", ""), row.get("recDate")
                )
                if ph in seen:
                    continue
                seen.add(ph)
                rows.append(row)
                idx_rows += 1

            log.debug("ca_window_fetched", index=index, start=str(a), end=str(b), rows=len(data))
        log.info("ca_index_fetched", index=index, new_rows=idx_rows)

    log.info(
        "ca_fetch_done",
        start=str(start),
        end=str(end),
        indices=list(s.ca_indices),
        unique_rows=len(rows),
    )
    return rows


def to_bronze_rows(api_rows: list[dict], scrape_date: dt.date) -> list[dict]:
    """Map API rows to ``bronze.corp_action_raw`` rows."""
    out: list[dict] = []
    for row in api_rows:
        ex_date = _parse_api_date(row.get("exDate"))
        purpose = (row.get("subject") or "").strip()
        if not purpose:
            continue
        out.append(
            {
                "scrape_date": scrape_date,
                "symbol": (row.get("symbol") or "").strip().upper() or None,
                "isin": (row.get("isin") or "").strip().upper() or None,
                "company": (row.get("comp") or "").strip() or None,
                "series": (row.get("series") or "").strip().upper() or None,
                "face_value": _face_value(row.get("faceVal")),
                "ex_date": ex_date,
                "record_date": _parse_api_date(row.get("recDate")),
                "bc_start_date": _parse_api_date(row.get("bcStartDate")),
                "bc_end_date": _parse_api_date(row.get("bcEndDate")),
                "nd_start_date": _parse_api_date(row.get("ndStartDate")),
                "nd_end_date": _parse_api_date(row.get("ndEndDate")),
                "ca_broadcast_date": _parse_api_date(row.get("caBroadcastDate")),
                "purpose_text": purpose,
                "payload": json.dumps(row),
                "payload_hash": payload_hash(
                    row.get("isin"), row.get("exDate"), purpose, row.get("recDate")
                ),
            }
        )
    return out
