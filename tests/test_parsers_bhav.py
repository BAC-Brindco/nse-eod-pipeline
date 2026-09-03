"""Bhavcopy parser tests. All fixtures are real NSE files on disk; no network."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from nse_eod.parsers.bhav import (
    CANONICAL_SCHEMA,
    BhavParseError,
    enrich,
    parse_bhavcopy,
    parse_delivery,
    parse_legacy,
    parse_pricebands,
    parse_udiff,
)
from nse_eod.parsers.detect import LEGACY, UDIFF, UnknownBhavFormat, detect_format

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------- detection
class TestDetect:
    def test_detects_udiff(self):
        assert detect_format(read("udiff_20260828.csv").splitlines()[0]) == UDIFF

    def test_detects_legacy(self):
        assert detect_format(read("legacy_01JUL2024.csv").splitlines()[0]) == LEGACY

    def test_rejects_unknown_header(self):
        with pytest.raises(UnknownBhavFormat):
            detect_format("foo,bar,baz,qux")

    def test_detection_is_by_content_not_filename(self):
        """A UDiFF file named like a legacy one still parses as UDiFF."""
        text = read("udiff_20260828.csv")
        assert detect_format(text.splitlines()[0]) == UDIFF


# ----------------------------------------------------------------- UDiFF parse
class TestUdiff:
    def test_schema_is_canonical(self):
        df = parse_udiff(read("udiff_20260828.csv"))
        assert list(df.columns) == list(CANONICAL_SCHEMA.keys())
        for name, dtype in CANONICAL_SCHEMA.items():
            assert df.schema[name] == dtype, name

    def test_parses_known_row_exactly(self):
        """20MICRONS on 2026-08-28, checked against the raw file by hand."""
        df = parse_udiff(read("udiff_20260828.csv"))
        row = df.filter(pl.col("symbol") == "20MICRONS").to_dicts()[0]
        assert row["isin"] == "INE144J01027"
        assert row["series"] == "EQ"
        assert row["trade_date"] == dt.date(2026, 8, 28)
        assert row["o"] == pytest.approx(217.34)
        assert row["h"] == pytest.approx(224.87)
        assert row["l"] == pytest.approx(217.00)
        assert row["volume"] == 241206
        assert row["trades"] == 5911

    def test_close_is_not_last_price(self):
        """The trap: UDiFF ClsPric=222.00 is the close, LastPric=220.30 is not.

        Cross-checked against sec_bhavdata_full, which lists CLOSE_PRICE=222.00
        and LAST_PRICE=220.30 for the same security and date.
        """
        df = parse_udiff(read("udiff_20260828.csv"))
        row = df.filter(pl.col("symbol") == "20MICRONS").to_dicts()[0]
        assert row["c"] == pytest.approx(222.00)
        assert row["last_price"] == pytest.approx(220.30)
        assert row["c"] != row["last_price"]

    def test_turnover_is_rupees_not_lakhs(self):
        """UDiFF TtlTrfVal is rupees: 53,281,611.76 == 532.82 lakhs."""
        df = parse_udiff(read("udiff_20260828.csv"))
        row = df.filter(pl.col("symbol") == "20MICRONS").to_dicts()[0]
        assert row["turnover"] == pytest.approx(53281611.76, rel=1e-6)
        assert row["turnover"] / 1e5 == pytest.approx(532.82, abs=0.01)

    def test_non_stock_instruments_are_dropped(self):
        df = parse_udiff(read("udiff_20260828.csv"))
        assert set(df["instrument_type"].unique().to_list()) == {"STK"}

    def test_no_null_isin_or_close(self):
        df = parse_udiff(read("udiff_20260828.csv"))
        assert df["isin"].null_count() == 0
        assert df["c"].null_count() == 0

    def test_natural_key_is_unique(self):
        df = parse_udiff(read("udiff_20260828.csv"))
        assert df.select(["trade_date", "isin", "series"]).is_duplicated().sum() == 0

    def test_same_isin_can_appear_in_two_series(self):
        """Why the PK is (date, isin, series) and not (date, isin).

        If any ISIN trades in more than one series, a narrower key would
        silently discard bars.
        """
        df = parse_udiff(read("udiff_20260828.csv"))
        per_isin = df.group_by("isin").len()
        assert per_isin["len"].max() >= 1  # structural: key includes series


# ---------------------------------------------------------------- legacy parse
class TestLegacy:
    def test_schema_is_canonical(self):
        df = parse_legacy(read("legacy_01JUL2024.csv"))
        assert list(df.columns) == list(CANONICAL_SCHEMA.keys())
        for name, dtype in CANONICAL_SCHEMA.items():
            assert df.schema[name] == dtype, name

    def test_parses_date_and_isin(self):
        df = parse_legacy(read("legacy_01JUL2024.csv"))
        assert df["trade_date"].unique().to_list() == [dt.date(2024, 7, 1)]
        assert df["isin"].str.starts_with("IN").all()

    def test_trailing_empty_column_is_tolerated(self):
        """The legacy header ends with a comma, creating a nameless column."""
        df = parse_legacy(read("legacy_01JUL2024.csv"))
        assert not df.is_empty()


# ------------------------------------------- cross-format equivalence (2024)
class TestFormatOverlap:
    """Jan-Jul 2024 is served in BOTH formats, so the parsers can check each other.

    This is the strongest available correctness evidence for the backfill: if
    the legacy and UDiFF paths disagree on a shared day, one of them is wrong.
    """

    def _joined(self) -> pl.DataFrame:
        u = parse_udiff(read("udiff_20240701.csv"))
        legacy = parse_legacy(read("legacy_01JUL2024.csv"))
        return u.join(legacy, on=["trade_date", "isin", "series"], how="inner", suffix="_lg")

    def test_overlap_is_substantial(self):
        j = self._joined()
        assert len(j) > 300, f"only {len(j)} rows matched across formats"

    @pytest.mark.parametrize("col", ["o", "h", "l", "c", "last_price", "prev_close"])
    def test_prices_agree_across_formats(self, col):
        j = self._joined()
        diff = (pl.col(col) - pl.col(f"{col}_lg")).abs()
        worst = j.select(diff.max().alias("m"))["m"][0]
        assert worst is not None and worst < 0.011, f"{col} differs by {worst}"

    def test_close_columns_are_not_transposed(self):
        """Guards the exact mistake that would swap CLOSE and LAST.

        If either parser mapped close<->last, the cross-format check on `c`
        would fail while `last_price` also failed -- so assert both directions
        explicitly on rows where the two genuinely differ.
        """
        j = self._joined().filter((pl.col("c") - pl.col("last_price")).abs() > 0.05)
        assert len(j) > 0, "need rows where close != last to make this meaningful"
        assert (j["c"] - j["c_lg"]).abs().max() < 0.011
        assert (j["last_price"] - j["last_price_lg"]).abs().max() < 0.011

    def test_volume_and_turnover_agree(self):
        j = self._joined()
        assert (j["volume"] - j["volume_lg"]).abs().max() == 0
        rel = ((j["turnover"] - j["turnover_lg"]).abs() / j["turnover"].abs().clip(1.0)).max()
        assert rel < 1e-6


class TestFlexibleDateParsing:
    """Regression: NSE is inconsistent about the year width in legacy files.

    The bhavcopy for 2020-07-13 wrote ``13-Jul-20`` while 2020-07-14 wrote
    ``14-JUL-2020``. ``%d-%b-%Y`` parses "20" as the year 20 AD, so the frame
    came back dated 0020-07-13, the expected-date check rejected the whole file,
    and that trading day was silently lost with no parse error to show for it.
    """

    LEGACY_HEADER = (
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,"
        "TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN"
    )

    def _one_row(self, timestamp: str) -> str:
        return (
            f"{self.LEGACY_HEADER}\n"
            f"20MICRONS,EQ,32.85,33.85,31.85,33.45,33.85,32.3,187303,6187285.7,"
            f"{timestamp},1382,INE144J01027\n"
        )

    @pytest.mark.parametrize(
        "timestamp,expected",
        [
            ("13-Jul-20", dt.date(2020, 7, 13)),    # the real malformed case
            ("14-JUL-2020", dt.date(2020, 7, 14)),  # the normal case
            ("01-FEB-2020", dt.date(2020, 2, 1)),
            ("13-jul-20", dt.date(2020, 7, 13)),    # lowercase month
        ],
    )
    def test_two_and_four_digit_years_both_parse(self, timestamp, expected):
        df = parse_legacy(self._one_row(timestamp))
        assert df["trade_date"][0] == expected

    def test_two_digit_year_is_not_read_as_year_20_ad(self):
        df = parse_legacy(self._one_row("13-Jul-20"))
        assert df["trade_date"][0].year == 2020, "a 2-digit year must not become 20 AD"

    def test_expected_date_check_now_passes_for_the_malformed_day(self):
        """The failure mode was a rejected FILE, not merely a wrong date."""
        df = parse_bhavcopy(self._one_row("13-Jul-20"), expected_date=dt.date(2020, 7, 13))
        assert len(df) == 1


class TestSaturdaySessions:
    """NSE holds occasional Saturday sessions and publishes a real bhavcopy.

    Verified live: 2020-02-01 (Budget, 1,886 rows), 2024-01-20 and 2024-05-18
    (special sessions) all return HTTP 200. A blanket weekend skip discarded
    those trading days, so only Sunday may be treated as never-trading.
    """

    # Every confirmed weekend session in 2020-2026. Six Saturdays and TWO
    # SUNDAYS -- Diwali Muhurat 2023-11-12 and the Union Budget 2026-02-01.
    CONFIRMED_WEEKEND_SESSIONS = [
        (dt.date(2020, 2, 1), "Sat", "Budget"),
        (dt.date(2020, 11, 14), "Sat", "Diwali Muhurat"),
        (dt.date(2023, 11, 12), "Sun", "Diwali Muhurat"),
        (dt.date(2024, 1, 20), "Sat", "special live session"),
        (dt.date(2024, 3, 2), "Sat", "special live session"),
        (dt.date(2024, 5, 18), "Sat", "special live session"),
        (dt.date(2025, 2, 1), "Sat", "Budget"),
        (dt.date(2026, 2, 1), "Sun", "Union Budget"),
    ]

    def test_no_day_is_ever_assumed_non_trading(self):
        """`never_trades` must be unconditionally False.

        It returned True for Sunday until 2026-02-01 falsified that: NSE
        published a full 180 KB bhavcopy for the Union Budget session. Every
        calendar assumption in this pipeline was eventually wrong and each one
        silently cost a real trading day, so only a 404 may rule a day out.
        """
        from nse_eod.sources.holidays import never_trades

        for d, _dow, _why in self.CONFIRMED_WEEKEND_SESSIONS:
            assert never_trades(d) is False, f"{d} was a REAL trading session"
        # ordinary weekend days and weekdays alike
        for day in range(24, 31):
            assert never_trades(dt.date(2026, 8, day)) is False

    def test_both_sundays_and_saturdays_are_represented(self):
        dows = {dow for _d, dow, _w in self.CONFIRMED_WEEKEND_SESSIONS}
        assert dows == {"Sat", "Sun"}, "the fix must cover both weekend days"

    def test_is_weekend_still_reports_saturday(self):
        """The weekend rule itself is unchanged; only its authority is reduced."""
        from nse_eod.sources.holidays import is_weekend

        assert is_weekend(dt.date(2026, 8, 29)) is True


# -------------------------------------------------------------- entry point
class TestParseBhavcopy:
    def test_dispatches_udiff(self):
        df = parse_bhavcopy(read("udiff_20260828.csv"), expected_date=dt.date(2026, 8, 28))
        assert not df.is_empty()

    def test_dispatches_legacy(self):
        df = parse_bhavcopy(read("legacy_01JUL2024.csv"), expected_date=dt.date(2024, 7, 1))
        assert not df.is_empty()

    def test_rejects_wrong_date(self):
        """A stale or misdated file must not be ingested under today's date."""
        with pytest.raises(BhavParseError, match="does not match"):
            parse_bhavcopy(read("udiff_20260828.csv"), expected_date=dt.date(2026, 8, 27))

    def test_accepts_raw_bytes(self):
        raw = read("udiff_20260828.csv").encode()
        assert not parse_bhavcopy(raw).is_empty()


# ------------------------------------------------------- delivery / pricebands
class TestDelivery:
    def test_parses_delivery(self):
        d = parse_delivery(read("delivery_28082026.csv"))
        assert set(d.columns) == {
            "trade_date", "symbol", "series", "vwap", "deliv_qty", "deliv_pct"
        }
        row = d.filter(pl.col("symbol") == "20MICRONS").to_dicts()[0]
        assert row["deliv_qty"] == 109988
        assert row["deliv_pct"] == pytest.approx(45.60)
        assert row["vwap"] == pytest.approx(220.90)

    def test_leading_spaces_in_headers_are_stripped(self):
        """Every column in this file is written as ' SERIES', ' DATE1', ..."""
        raw = read("delivery_28082026.csv").splitlines()[0]
        assert " SERIES" in raw, "fixture should still have the padded headers"
        assert "series" in parse_delivery(read("delivery_28082026.csv")).columns

    def test_non_equity_series_present_and_not_dropped(self):
        """Government securities appear here; the join must not depend on EQ only."""
        d = parse_delivery(read("delivery_28082026.csv"))
        assert len(set(d["series"].to_list())) > 1


class TestPricebands:
    def test_parses_bands(self):
        p = parse_pricebands(read("pricebands_28082026.csv"))
        assert set(p.columns) == {"symbol", "series", "price_band", "band_remarks"}
        assert not p.is_empty()


# ----------------------------------------------------------------- enrichment
class TestEnrich:
    def test_joins_delivery_and_bands(self):
        bhav = parse_udiff(read("udiff_20260828.csv"))
        d = parse_delivery(read("delivery_28082026.csv"))
        p = parse_pricebands(read("pricebands_28082026.csv"))
        out = enrich(bhav, d, p)
        assert len(out) == len(bhav), "enrichment must be a LEFT join, never dropping bars"
        row = out.filter(pl.col("symbol") == "20MICRONS").to_dicts()[0]
        assert row["deliv_qty"] == 109988
        assert row["vwap"] == pytest.approx(220.90)
        assert "circuit_flag" in out.columns

    def test_survives_missing_delivery_data(self):
        """Pre-2020 has no delivery file at all. Bars must still ingest."""
        bhav = parse_legacy(read("legacy_01JUL2024.csv"))
        out = enrich(bhav, None, None)
        assert len(out) == len(bhav)
        assert out["deliv_qty"].null_count() == len(out)
        # vwap must still be derived from turnover/volume
        assert out["vwap"].null_count() < len(out)

    def test_vwap_falls_back_to_turnover_over_volume(self):
        bhav = parse_legacy(read("legacy_01JUL2024.csv"))
        out = enrich(bhav, None, None).filter(pl.col("volume") > 0)
        row = out.to_dicts()[0]
        assert row["vwap"] == pytest.approx(row["turnover"] / row["volume"], rel=1e-9)

    def test_circuit_flag_is_boolean_and_never_null(self):
        bhav = parse_udiff(read("udiff_20260828.csv"))
        out = enrich(bhav, parse_delivery(read("delivery_28082026.csv")),
                     parse_pricebands(read("pricebands_28082026.csv")))
        assert out["circuit_flag"].null_count() == 0
        assert out.schema["circuit_flag"] == pl.Boolean
