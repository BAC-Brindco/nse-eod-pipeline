"""Security master from EQUITY_L.csv, plus ISIN-keyed symbol-history maintenance.

    GET https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
    SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,
    MARKET LOT, ISIN NUMBER, FACE VALUE

EQUITY_L.csv lists only CURRENTLY listed securities, so it cannot be the sole
source: a symbol that delists disappears from it. Using it alone would silently
introduce survivorship bias into every downstream backtest. The master is
therefore built from the union of EQUITY_L and everything ever seen in a
bhavcopy, with ``status`` transitioning to ``delisted`` rather than the row
being removed.
"""

from __future__ import annotations

import datetime as dt
import io

import polars as pl

from ..logging_setup import get_logger
from .http import ARCHIVES, NSESession

log = get_logger(__name__)

EQUITY_L_URL = f"{ARCHIVES}/content/equities/EQUITY_L.csv"

# No bar for this many calendar days => presumed delisted/suspended.
STALE_DAYS = 90


def fetch_equity_list(session: NSESession) -> pl.DataFrame:
    content = session.get_bytes(EQUITY_L_URL)
    session.archive(content, "master/EQUITY_L.csv")
    return parse_equity_list(content.decode("utf-8", errors="replace"))


def parse_equity_list(csv_text: str) -> pl.DataFrame:
    df = pl.read_csv(io.StringIO(csv_text), infer_schema_length=0)
    df = df.rename({c: c.strip().upper() for c in df.columns})
    colmap = {
        "SYMBOL": "symbol",
        "NAME OF COMPANY": "name",
        "SERIES": "series",
        "DATE OF LISTING": "listing_date",
        "ISIN NUMBER": "isin",
        "FACE VALUE": "face_value",
    }
    missing = set(colmap) - set(df.columns)
    if missing:
        raise ValueError(f"EQUITY_L.csv missing columns: {sorted(missing)}")

    return (
        df.select(
            pl.col("ISIN NUMBER").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("isin"),
            pl.col("SYMBOL").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
            pl.col("NAME OF COMPANY").cast(pl.Utf8).str.strip_chars().alias("name"),
            pl.col("SERIES").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("series"),
            pl.col("DATE OF LISTING")
            .cast(pl.Utf8)
            .str.strip_chars()
            .str.to_date("%d-%b-%Y", strict=False)
            .alias("listing_date"),
            pl.col("FACE VALUE")
            .cast(pl.Utf8)
            .str.strip_chars()
            .cast(pl.Float64, strict=False)
            .alias("face_value"),
        )
        .filter(pl.col("isin").is_not_null() & (pl.col("isin").str.len_chars() > 0))
        .unique(subset=["isin"], keep="first")
    )


def merge_symbol_history(
    existing: list | None, symbol: str, seen_date: dt.date
) -> tuple[list, bool]:
    """Fold a sighting into a symbol-history list.

    Returns ``(history, changed)``. Each entry is
    ``{"symbol": ..., "first_seen": ..., "last_seen": ...}``.

    Resolving on ISIN means a rename (MINDTREE -> LTIM) appends a new entry
    rather than losing the old identity, so a backtest can still resolve the
    ticker a signal was generated under at the time.
    """
    hist = list(existing or [])
    d = seen_date.isoformat()
    for entry in hist:
        if entry.get("symbol") == symbol:
            changed = False
            if d < entry.get("first_seen", d):
                entry["first_seen"] = d
                changed = True
            if d > entry.get("last_seen", d):
                entry["last_seen"] = d
                changed = True
            return hist, changed
    hist.append({"symbol": symbol, "first_seen": d, "last_seen": d})
    hist.sort(key=lambda e: (e.get("first_seen") or "", e.get("symbol") or ""))
    return hist, True


def classify_isin(isin: str | None) -> str:
    """Classify an instrument by ISIN prefix.

    Indian ISINs encode the instrument class in characters 3-4:
        INE / IN9 / IN0  -> company equity or debt
        INF              -> mutual fund / ETF UNITS, not shares

    This matters because ETF units trade in series ``EQ`` and are therefore
    indistinguishable from equity on series alone. Observed live: GOLDADD
    (INF740KA1ZP2) and SILVERADD (INF740KA1ZQ0) both executed ~10:1 unit splits
    on 2026-08-28, and NSE does NOT publish ETF unit splits in the equity
    corporate-actions feed -- so their adjusted series showed a genuine
    unexplained -90 % move. They are not equities and must not sit in the
    tradeable universe.
    """
    s = (isin or "").upper().strip()
    if s.startswith("INF"):
        return "ETF_MF"
    return "EQUITY"


def classify_series(series: str | None) -> str:
    """Bucket a series code.

    EQ is the main board. BE/BZ are trade-for-trade (surveillance) settlement,
    SM/ST are the SME platform, and IV/RR are InvIT/REIT units. Everything is
    captured and flagged; which buckets enter the tradeable universe is set by
    ``Settings.universe_series`` (default EQ + SM + ST).
    """
    s = (series or "").upper().strip()
    if s == "EQ":
        return "EQ"
    if s in ("BE", "BZ"):
        return s
    if s in ("BT", "T2T"):
        return "T2T"
    if s in ("SM", "ST"):
        return s
    if s in ("IV", "RR", "MF"):
        return s
    if s in ("GS", "GB", "TB", "SG", "N0", "N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "N9",
             "NA", "NB", "NC", "ND", "NE", "NF", "NG", "NH", "NI", "NJ", "NK", "NL",
             "Y1", "YA", "YB", "YC", "W1", "W2", "W3", "AF", "AG", "AH", "AI", "AJ"):
        return "DEBT"
    return "OTHER"


# Series that reach the gold panel at all. Debt, government securities and
# similar are ingested to bronze but are not equities and are excluded here.
# BE/BZ are included so a symbol placed under surveillance keeps a continuous
# price series, even though it is not in the tradeable universe.
GOLD_SERIES = frozenset({"EQ", "BE", "BZ", "T2T", "SM", "ST"})

# The TRADEABLE universe is configured, not hardcoded: see
# Settings.universe_series (default EQ + SM + ST). This constant remains only as
# the conservative fallback for callers without a Settings object.
TRADEABLE_SERIES = frozenset({"EQ"})
