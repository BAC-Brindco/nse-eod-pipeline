"""Fetch NSE's daily all-index close file. Carries India VIX and the Nifty 50 level.

ENDPOINT, VERIFIED LIVE ON 2026-09-03
    https://nsearchives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv
    200 for every trading day probed from 2018-06-29 to 2026-09-01
    404 on Saturday/Sunday, i.e. absence means "no session", not "fetch failed"
    ~165 index rows per file; needs no cookie (nsearchives never does)

TWO ENDPOINTS THAT DO NOT WORK, RECORDED SO NOBODY RETRIES THEM
    /api/historical/vixhistory   503 after full retry/backoff. Dead.
    /api/allIndices              200, and it does carry INDIA VIX -- but it is an
                                 INTRADAY snapshot (observed timestamp 09:50 during
                                 a live session). Using it for a daily series would
                                 silently record a mid-session print as the close.

THE "-" PROBLEM
    India VIX prints "-" for volume, turnover, P/E, P/B and dividend yield, because
    a volatility index has none of those. Parsing "-" as 0 would put a real number
    where there is no measurement, and a downstream liquidity filter would believe
    it. Every such field becomes NULL.

NUMBER FORMATTING
    The file writes leading-dot decimals ("-.1", ".99") and thousands-free integers.
    float() handles both; the parser rejects anything it cannot convert rather than
    coercing, so a format change surfaces as a parse error instead of a wrong price.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from .http import ARCHIVES, WWW, NSENotFound, NSESession

log = get_logger(__name__)

# Column order in the file, verified 2026-09-03:
#   Index Name, Index Date, Open, High, Low, Close, Points Change, Change(%),
#   Volume, Turnover (Rs. Cr.), P/E, P/B, Div Yield
EXPECTED_HEADER_START = ("index name", "index date")
N_COLS = 13

_WS = re.compile(r"\s+")


def url_for(d: dt.date) -> str:
    return f"{ARCHIVES}/content/indices/ind_close_all_{d:%d%m%Y}.csv"


def normalise_key(name: str) -> str:
    """Casing- and spacing-insensitive join key.

    NSE prints "Nifty 50", "NIFTY Midcap 100" and "India VIX" in the same file, so a
    consumer matching on the raw string has to know which casing was used on which
    day. Upper-case and collapse whitespace once, here.
    """
    return _WS.sub(" ", name.strip()).upper()


def _num(raw: str | None) -> float | None:
    """Parse a value cell. "-" and "" are genuinely absent, not zero."""
    if raw is None:
        return None
    t = raw.strip()
    if t in ("", "-", "--", "NA", "N/A"):
        return None
    t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _int(raw: str | None) -> int | None:
    v = _num(raw)
    return None if v is None else int(v)


def fetch(d: dt.date, session: NSESession) -> bytes | None:
    """Raw CSV for one date, or None when NSE has no file (non-trading day)."""
    try:
        return session.get_bytes(url_for(d), referer=f"{WWW}/all-reports")
    except NSENotFound:
        log.debug("index_close_absent", date=str(d))
        return None


def parse(raw: bytes, d: dt.date, source_file: str) -> list[dict]:
    """CSV -> one dict per index row. Raises on a header we do not recognise.

    The header check is deliberate: NSE has changed file layouts before (the equity
    bhavcopy moved to UDiFF mid-2024), and positional parsing against a changed
    header would write high values into the low column without erroring.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"{source_file}: empty file")

    header = [c.strip().lower() for c in rows[0]]
    if tuple(header[:2]) != EXPECTED_HEADER_START:
        raise ValueError(
            f"{source_file}: unexpected header {header[:4]!r}; "
            f"expected to start with {EXPECTED_HEADER_START}"
        )
    if len(header) != N_COLS:
        log.warning(
            "index_close_column_count_changed",
            date=str(d), expected=N_COLS, got=len(header), header=header,
        )

    out: list[dict] = []
    for r in rows[1:]:
        if len(r) < 6 or not r[0].strip():
            continue
        cells = (list(r) + [""] * N_COLS)[:N_COLS]
        name = cells[0].strip()

        # The file's own date column is cross-checked against the date we asked for,
        # so a mis-served or redirected file cannot be filed under the wrong day.
        #
        # NSE IS INCONSISTENT ABOUT DAY/MONTH ORDER.
        # Observed 2026-09-03: ind_close_all_06042023.csv (URL = 6 April 2023) has a
        # date column reading "04-06-2023", i.e. MM-DD-YYYY. Three April 2023 files do
        # this. Read as DD-MM-YYYY that is 4 June, so a strict check refuses the file
        # -- correctly, since silently accepting would have filed April prices under a
        # June date. Nifty 50 at 17,599 in that file confirms April, not June.
        #
        # The URL is the independent source of truth, so an ambiguous column is
        # resolved AGAINST it: if the swapped reading matches the requested date,
        # accept and record which format the file used. Anything matching NEITHER
        # ordering is still refused -- the protection is narrowed, not removed.
        file_date, fmt_note = _reconcile_date(cells[1], d)
        if file_date != d:
            raise ValueError(
                f"{source_file}: row date column {cells[1]!r} cannot be reconciled "
                f"with requested {d} under either DD-MM-YYYY or MM-DD-YYYY"
            )
        if fmt_note and name == rows[1][0].strip():
            log.info("index_close_date_format", date=str(d), file=source_file, note=fmt_note)

        payload = "|".join(cells)
        out.append(
            {
                "trade_date": d,
                "index_name": name,
                "index_key": normalise_key(name),
                "open_txt": cells[2].strip() or None,
                "high_txt": cells[3].strip() or None,
                "low_txt": cells[4].strip() or None,
                "close_txt": cells[5].strip() or None,
                "points_chg": cells[6].strip() or None,
                "pct_chg": cells[7].strip() or None,
                "volume_txt": cells[8].strip() or None,
                "turnover_txt": cells[9].strip() or None,
                "pe_txt": cells[10].strip() or None,
                "pb_txt": cells[11].strip() or None,
                "div_yield_txt": cells[12].strip() or None,
                "source_file": source_file,
                "row_hash": hashlib.sha256(payload.encode()).hexdigest()[:32],
            }
        )
    return out


def _parse_date(raw: str) -> dt.date | None:
    """Best-effort DD-MM-YYYY parse. See _reconcile_date for the ambiguous case."""
    t = (raw or "").strip()
    for fmt in ("%d-%m-%Y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _reconcile_date(raw: str, requested: dt.date) -> tuple[dt.date | None, str | None]:
    """Resolve the date column against the date the URL asked for.

    Returns (date, note). The note is set only when the file used the non-standard
    month-first ordering, so it can be logged once rather than silently absorbed.

    Order matters: DD-MM-YYYY is tried FIRST and wins whenever it matches, so the
    normal case is never reinterpreted. The month-first reading is only consulted
    when the day-first one disagrees with the URL.
    """
    t = (raw or "").strip()
    if not t:
        # No date column at all: fall back to the URL rather than refusing, since the
        # URL is what selected the file.
        return requested, None

    day_first = _parse_date(t)
    if day_first == requested:
        return day_first, None

    for fmt in ("%m-%d-%Y", "%m/%d/%Y"):
        try:
            month_first = dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        if month_first == requested:
            return month_first, f"date column {t!r} is MM-DD-YYYY, not DD-MM-YYYY"

    return day_first, None


def to_silver(bronze_rows: list[dict]) -> list[dict]:
    """Typed rows for silver.index_daily. Pure -- replayable from bronze."""
    return [
        {
            "trade_date": r["trade_date"],
            "index_key": r["index_key"],
            "index_name": r["index_name"],
            "open": _num(r["open_txt"]),
            "high": _num(r["high_txt"]),
            "low": _num(r["low_txt"]),
            "close": _num(r["close_txt"]),
            "points_chg": _num(r["points_chg"]),
            "pct_chg": _num(r["pct_chg"]),
            "volume": _int(r["volume_txt"]),
            "turnover_cr": _num(r["turnover_txt"]),
            "pe": _num(r["pe_txt"]),
            "pb": _num(r["pb_txt"]),
            "div_yield": _num(r["div_yield_txt"]),
        }
        for r in bronze_rows
    ]


def fetch_and_parse(
    d: dt.date, session: NSESession, settings: Settings | None = None
) -> tuple[list[dict], list[dict]] | None:
    """(bronze_rows, silver_rows) for one date, or None if there is no file."""
    s = settings or get_settings()
    raw = fetch(d, session)
    if raw is None:
        return None
    name = url_for(d).rsplit("/", 1)[-1]
    if s.keep_raw_files:
        session.archive(raw, name)
    bronze_rows = parse(raw, d, name)
    return bronze_rows, to_silver(bronze_rows)
