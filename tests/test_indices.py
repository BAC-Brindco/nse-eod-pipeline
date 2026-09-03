"""Index close-file parser: the "-" trap, header drift, and date mis-service.

Every fixture below is real text copied from the live file (verified 2026-09-03), so
these tests fail if NSE changes the layout rather than passing on a synthetic ideal.
"""

from __future__ import annotations

import datetime as dt

import pytest

from nse_eod.sources import indices as src

HEADER = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    "Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),"
    "P/E,P/B,Div Yield"
)
# Verbatim rows from ind_close_all_01092026.csv
VIX_ROW = "India VIX,01-09-2026,11.19,12.1225,9.2475,11.49,0.3,2.7,-,-,-,-,-"
N50_ROW = (
    "Nifty 50,01-09-2026,24077.55,24143.15,23952.55,24055.8,-24.6,-.1,"
    "334207690,27996.18,20.34,2.92,1.17"
)
D = dt.date(2026, 9, 1)


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join((HEADER, *rows)) + "\n").encode("utf-8")


# ------------------------------------------------------------------ the "-" trap
def test_vix_non_applicable_fields_are_null_never_zero():
    """India VIX has no volume, turnover, P/E, P/B or dividend yield.

    Parsing "-" as 0 would put a real number where there is no measurement, and a
    downstream liquidity filter would believe it.
    """
    bronze = src.parse(csv_bytes(VIX_ROW), D, "f.csv")
    silver = src.to_silver(bronze)[0]
    assert silver["close"] == pytest.approx(11.49)
    assert silver["open"] == pytest.approx(11.19)
    assert silver["high"] == pytest.approx(12.1225)
    assert silver["low"] == pytest.approx(9.2475)
    for field in ("volume", "turnover_cr", "pe", "pb", "div_yield"):
        assert silver[field] is None, f"{field} became {silver[field]!r} instead of NULL"


def test_bronze_keeps_the_dash_verbatim():
    """Bronze is the file's own text, so a parser fix is replayable without refetching."""
    bronze = src.parse(csv_bytes(VIX_ROW), D, "f.csv")[0]
    assert bronze["volume_txt"] == "-"
    assert bronze["close_txt"] == "11.49"


def test_an_index_with_real_volume_still_parses_it():
    silver = src.to_silver(src.parse(csv_bytes(N50_ROW), D, "f.csv"))[0]
    assert silver["close"] == pytest.approx(24055.8)
    assert silver["volume"] == 334207690
    assert silver["turnover_cr"] == pytest.approx(27996.18)
    assert silver["pe"] == pytest.approx(20.34)


# ------------------------------------------------------- number format oddities
def test_leading_dot_decimals_parse():
    """The file writes "-.1" and ".99" rather than "-0.1" and "0.99"."""
    silver = src.to_silver(src.parse(csv_bytes(N50_ROW), D, "f.csv"))[0]
    assert silver["pct_chg"] == pytest.approx(-0.1)


def test_unparseable_value_becomes_null_not_a_guess():
    row = "Weird Index,01-09-2026,abc,1,2,3,0,0,-,-,-,-,-"
    silver = src.to_silver(src.parse(csv_bytes(row), D, "f.csv"))[0]
    assert silver["open"] is None
    assert silver["close"] == pytest.approx(3.0)


# --------------------------------------------------------------- key normalisation
def test_index_keys_are_case_and_space_normalised():
    """The same file prints "Nifty 50", "NIFTY Midcap 100" and "India VIX".

    Matching on the raw string would mean knowing which casing was used on which day.
    """
    assert src.normalise_key("Nifty 50") == "NIFTY 50"
    assert src.normalise_key("  india   vix ") == "INDIA VIX"
    assert src.normalise_key("NIFTY Midcap 100") == "NIFTY MIDCAP 100"


def test_display_name_is_preserved_alongside_the_key():
    bronze = src.parse(csv_bytes(VIX_ROW), D, "f.csv")[0]
    assert bronze["index_name"] == "India VIX"
    assert bronze["index_key"] == "INDIA VIX"


# ------------------------------------------------------------------ header drift
def test_unrecognised_header_raises_rather_than_parsing_positionally():
    """NSE has changed layouts before (equity bhavcopy -> UDiFF in 2024).

    Positional parsing against a reordered header would write highs into the low
    column without erroring, which is the worst possible outcome.
    """
    bad = b"Symbol,Date,Price\nX,01-09-2026,1\n"
    with pytest.raises(ValueError, match="unexpected header"):
        src.parse(bad, D, "f.csv")


def test_empty_file_raises():
    with pytest.raises(ValueError, match="empty file"):
        src.parse(b"", D, "f.csv")


# --------------------------------------------------------- wrong-date protection
def test_a_file_served_for_the_wrong_date_is_refused():
    """Guards against NSE serving a stale or redirected file under our URL.

    Without this the row would be filed under the date we asked for, silently
    duplicating another session's values. 2026-08-28 cannot be reached from
    "01-09-2026" under either day-first or month-first reading.
    """
    with pytest.raises(ValueError, match="cannot be reconciled"):
        src.parse(csv_bytes(VIX_ROW), dt.date(2026, 8, 28), "f.csv")


def test_month_first_date_column_is_reconciled_against_the_url():
    """NSE wrote MM-DD-YYYY in three April 2023 files. Observed, not hypothetical.

    ind_close_all_06042023.csv (URL = 6 April 2023) has a date column of "04-06-2023".
    Read day-first that is 4 June, so a strict check refuses the file. The URL is the
    independent source of truth, so the swapped reading is accepted when it matches.
    """
    row = "Nifty 50,04-06-2023,17533.85,17638.7,17502.85,17599.15,42.1,0.24,1,1,1,1,1"
    bronze = src.parse(csv_bytes(row), dt.date(2023, 4, 6), "ind_close_all_06042023.csv")
    assert bronze[0]["trade_date"] == dt.date(2023, 4, 6)


def test_day_first_always_wins_when_it_matches():
    """The normal case must never be reinterpreted as month-first.

    "01-09-2026" is 1 September day-first and 9 January month-first; both are valid
    dates, so ordering of the attempts is what decides. Day-first is tried first.
    """
    bronze = src.parse(csv_bytes(VIX_ROW), dt.date(2026, 9, 1), "f.csv")
    assert bronze[0]["trade_date"] == dt.date(2026, 9, 1)


def test_a_date_matching_neither_ordering_is_still_refused():
    """The protection is narrowed, not removed."""
    row = "X,25-12-2024,1,1,1,1,0,0,-,-,-,-,-"
    with pytest.raises(ValueError, match="cannot be reconciled"):
        src.parse(csv_bytes(row), dt.date(2024, 1, 2), "f.csv")


def test_the_requested_date_is_what_gets_stored():
    bronze = src.parse(csv_bytes(VIX_ROW, N50_ROW), D, "f.csv")
    assert all(r["trade_date"] == D for r in bronze)


# ------------------------------------------------------------------------- misc
def test_blank_and_short_rows_are_skipped_not_crashed_on():
    rows = (VIX_ROW, "", ",,,", "Short Row,01-09-2026,1")
    bronze = src.parse(csv_bytes(*rows), D, "f.csv")
    assert [r["index_key"] for r in bronze] == ["INDIA VIX"]


def test_url_uses_ddmmyyyy():
    assert src.url_for(dt.date(2026, 9, 1)).endswith("ind_close_all_01092026.csv")
    assert src.url_for(dt.date(2020, 1, 1)).endswith("ind_close_all_01012020.csv")


def test_row_hash_is_stable_and_content_sensitive():
    a = src.parse(csv_bytes(VIX_ROW), D, "f.csv")[0]["row_hash"]
    b = src.parse(csv_bytes(VIX_ROW), D, "f.csv")[0]["row_hash"]
    c = src.parse(csv_bytes(VIX_ROW.replace("11.49", "11.50")), D, "f.csv")[0]["row_hash"]
    assert a == b
    assert a != c


def test_dead_endpoints_are_documented_so_nobody_retries_them():
    """The 503 vixhistory API and the intraday allIndices trap cost real probing."""
    doc = src.__doc__ or ""
    assert "vixhistory" in doc and "503" in doc
    assert "allIndices" in doc and "INTRADAY" in doc.upper()
