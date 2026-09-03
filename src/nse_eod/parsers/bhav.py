"""Parse UDiFF and legacy bhavcopies into ONE canonical polars frame.

Both parsers must emit exactly ``CANONICAL_SCHEMA`` so everything downstream is
format-agnostic. The two formats overlap in Jan-Jul 2024, which the backfill
uses to cross-check the parsers against each other.

Column mapping traps, verified on 20MICRONS for 2026-08-28:

* UDiFF ``ClsPric`` is the CLOSE and ``LastPric`` is the LAST TRADE
  (222.00 and 220.30 respectively). The legacy file lists them in the opposite
  order (``CLOSE``, ``LAST``). Swapping them silently corrupts every bar.
* UDiFF ``TtlTrfVal`` is in RUPEES; the delivery file's ``TURNOVER_LACS`` is in
  LAKHS. Canonical stores rupees.
* UDiFF carries ISIN but has NO vwap and NO delivery columns; those come from
  ``sec_bhavdata_full`` via a symbol+series join.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile

import polars as pl

from .detect import LEGACY, UDIFF, detect_format_from_text

# The canonical bronze frame. Order is fixed so tests can compare schemas.
CANONICAL_SCHEMA: dict[str, pl.DataType] = {
    "trade_date": pl.Date,
    "isin": pl.Utf8,
    "symbol": pl.Utf8,
    "series": pl.Utf8,
    "o": pl.Float64,
    "h": pl.Float64,
    "l": pl.Float64,
    "c": pl.Float64,
    "last_price": pl.Float64,
    "prev_close": pl.Float64,
    "volume": pl.Int64,
    "turnover": pl.Float64,
    "trades": pl.Int64,
    "instrument_type": pl.Utf8,
}

# UDiFF FinInstrmTp values that are cash-segment stocks. Everything else in the
# CM UDiFF file (index derivatives etc.) is not an equity bar.
_UDIFF_STOCK_TYPES = {"STK"}


class BhavParseError(ValueError):
    pass


def unzip_single_csv(content: bytes) -> str:
    """Return the text of the single CSV inside a bhavcopy zip.

    Accepts raw CSV bytes too, so callers do not need to care which they have.
    """
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise BhavParseError(f"no CSV inside zip: {z.namelist()}")
            return z.read(names[0]).decode("utf-8", errors="replace")
    return content.decode("utf-8", errors="replace")


def _clean_headers(df: pl.DataFrame) -> pl.DataFrame:
    """Strip whitespace from column names.

    The legacy header ends with a trailing comma, producing an empty final
    column, and ``sec_bhavdata_full`` pads every name with a leading space.
    """
    return df.rename({c: c.strip() for c in df.columns})


def _flexible_dmy(col: str) -> pl.Expr:
    """Parse a ``DD-Mon-YYYY`` date that may carry a TWO-digit year.

    NSE is not consistent about this. Verified: the legacy bhavcopy for
    2020-07-13 wrote ``13-Jul-20`` while 2020-07-14 wrote ``14-JUL-2020``.
    ``%d-%b-%Y`` happily parses "20" as the year 20 AD, so the bar came back
    dated 0020-07-13, the expected-date check rejected the whole file, and that
    trading day was lost -- with no parse error to show for it.

    Normalising the year textually before parsing is safer than post-hoc
    correction: it fails loudly on a genuinely unrecognised shape instead of
    inventing a century.
    """
    s = pl.col(col).cast(pl.Utf8).str.strip_chars()
    day = s.str.extract(r"^(\d{1,2})", 1)
    mon = s.str.extract(r"^\d{1,2}[-/]([A-Za-z]{3})", 1)
    yr = s.str.extract(r"[-/](\d{2,4})$", 1)
    yr4 = (
        pl.when(yr.str.len_chars() == 2)
        .then(pl.lit("20") + yr)
        .otherwise(yr)
    )
    return (
        pl.concat_str([day, mon.str.to_titlecase(), yr4], separator="-")
        .str.to_date("%d-%b-%Y", strict=False)
    )


def _to_float(col: str) -> pl.Expr:
    return (
        pl.col(col)
        .cast(pl.Utf8)
        .str.strip_chars()
        .replace({"": None, "-": None, "nan": None})
        .cast(pl.Float64, strict=False)
    )


def _to_int(col: str) -> pl.Expr:
    return (
        pl.col(col)
        .cast(pl.Utf8)
        .str.strip_chars()
        .replace({"": None, "-": None, "nan": None})
        .cast(pl.Float64, strict=False)
        .cast(pl.Int64, strict=False)
    )


# --------------------------------------------------------------------- UDiFF
def parse_udiff(csv_text: str) -> pl.DataFrame:
    """Parse a UDiFF common bhavcopy into the canonical frame."""
    df = _clean_headers(pl.read_csv(io.StringIO(csv_text), infer_schema_length=0))
    required = {"TradDt", "ISIN", "TckrSymb", "SctySrs", "ClsPric"}
    missing = required - set(df.columns)
    if missing:
        raise BhavParseError(f"UDiFF missing columns: {sorted(missing)}")

    if "FinInstrmTp" in df.columns:
        df = df.filter(pl.col("FinInstrmTp").cast(pl.Utf8).str.strip_chars().is_in(_UDIFF_STOCK_TYPES))

    out = df.select(
        pl.col("TradDt")
        .cast(pl.Utf8)
        .str.strip_chars()
        .str.to_date("%Y-%m-%d", strict=False)
        .alias("trade_date"),
        pl.col("ISIN").cast(pl.Utf8).str.strip_chars().alias("isin"),
        pl.col("TckrSymb").cast(pl.Utf8).str.strip_chars().alias("symbol"),
        pl.col("SctySrs").cast(pl.Utf8).str.strip_chars().alias("series"),
        _to_float("OpnPric").alias("o"),
        _to_float("HghPric").alias("h"),
        _to_float("LwPric").alias("l"),
        # ClsPric is the CLOSE. LastPric is the last traded price. Not the same.
        _to_float("ClsPric").alias("c"),
        _to_float("LastPric").alias("last_price"),
        _to_float("PrvsClsgPric").alias("prev_close"),
        _to_int("TtlTradgVol").alias("volume"),
        _to_float("TtlTrfVal").alias("turnover"),  # already rupees
        _to_int("TtlNbOfTxsExctd").alias("trades"),
        pl.col("FinInstrmTp").cast(pl.Utf8).str.strip_chars().alias("instrument_type")
        if "FinInstrmTp" in df.columns
        else pl.lit("STK").alias("instrument_type"),
    )
    return _finalize(out)


# -------------------------------------------------------------------- legacy
def parse_legacy(csv_text: str) -> pl.DataFrame:
    """Parse a legacy ``cm<DDMMMYYYY>bhav.csv`` into the canonical frame.

    Header: SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,
            TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,
    """
    df = _clean_headers(pl.read_csv(io.StringIO(csv_text), infer_schema_length=0))
    df = df.rename({c: c.upper() for c in df.columns})
    required = {"SYMBOL", "SERIES", "CLOSE", "TIMESTAMP", "ISIN"}
    missing = required - set(df.columns)
    if missing:
        raise BhavParseError(f"legacy missing columns: {sorted(missing)}")

    out = df.select(
        _flexible_dmy("TIMESTAMP").alias("trade_date"),
        pl.col("ISIN").cast(pl.Utf8).str.strip_chars().alias("isin"),
        pl.col("SYMBOL").cast(pl.Utf8).str.strip_chars().alias("symbol"),
        pl.col("SERIES").cast(pl.Utf8).str.strip_chars().alias("series"),
        _to_float("OPEN").alias("o"),
        _to_float("HIGH").alias("h"),
        _to_float("LOW").alias("l"),
        _to_float("CLOSE").alias("c"),
        _to_float("LAST").alias("last_price"),
        _to_float("PREVCLOSE").alias("prev_close"),
        _to_int("TOTTRDQTY").alias("volume"),
        _to_float("TOTTRDVAL").alias("turnover"),  # rupees
        _to_int("TOTALTRADES").alias("trades"),
        pl.lit("STK").alias("instrument_type"),
    )
    return _finalize(out)


def _finalize(df: pl.DataFrame) -> pl.DataFrame:
    """Drop unusable rows and enforce the canonical dtypes."""
    df = df.filter(
        pl.col("isin").is_not_null()
        & (pl.col("isin").str.len_chars() > 0)
        & pl.col("trade_date").is_not_null()
        & pl.col("c").is_not_null()
    )
    df = df.with_columns(
        pl.col("isin").str.to_uppercase(),
        pl.col("symbol").str.to_uppercase(),
        pl.col("series").str.to_uppercase(),
    )
    # Same (date, isin, series) twice would be an NSE error; keep the first.
    df = df.unique(subset=["trade_date", "isin", "series"], keep="first")
    return df.select(
        [pl.col(name).cast(dtype) for name, dtype in CANONICAL_SCHEMA.items()]
    ).sort(["isin", "series"])


def parse_bhavcopy(content: bytes | str, expected_date: dt.date | None = None) -> pl.DataFrame:
    """Detect the format and parse. The single entry point for bhavcopy files."""
    text = unzip_single_csv(content) if isinstance(content, bytes) else content
    fmt = detect_format_from_text(text)
    df = parse_udiff(text) if fmt == UDIFF else parse_legacy(text)
    if df.is_empty():
        raise BhavParseError(f"{fmt} bhavcopy parsed to zero usable rows")
    if expected_date is not None:
        dates = df["trade_date"].unique().to_list()
        if dates != [expected_date]:
            raise BhavParseError(
                f"bhavcopy trade_date {dates} does not match requested {expected_date}"
            )
    return df


# --------------------------------------------------- sec_bhavdata_full (delivery)
def parse_delivery(csv_text: str) -> pl.DataFrame:
    """Parse ``sec_bhavdata_full_<DDMMYYYY>.csv``.

    Supplies the two things UDiFF lacks: VWAP (``AVG_PRICE``) and delivery
    (``DELIV_QTY``, ``DELIV_PER``). Has no ISIN, so it joins on symbol+series.
    Every header is padded with a leading space in the source file.
    """
    df = _clean_headers(pl.read_csv(io.StringIO(csv_text), infer_schema_length=0))
    required = {"SYMBOL", "SERIES", "DATE1"}
    missing = required - set(df.columns)
    if missing:
        raise BhavParseError(f"delivery file missing columns: {sorted(missing)}")

    return df.select(
        _flexible_dmy("DATE1").alias("trade_date"),
        pl.col("SYMBOL").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.col("SERIES").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("series"),
        _to_float("AVG_PRICE").alias("vwap"),
        _to_int("DELIV_QTY").alias("deliv_qty"),
        _to_float("DELIV_PER").alias("deliv_pct"),
    ).filter(pl.col("trade_date").is_not_null()).unique(
        subset=["trade_date", "symbol", "series"], keep="first"
    )


# ------------------------------------------------------- sec_list (price bands)
def parse_pricebands(csv_text: str) -> pl.DataFrame:
    """Parse ``sec_list_<DDMMYYYY>.csv``: Symbol,Series,Security Name,Band,Remarks."""
    df = _clean_headers(pl.read_csv(io.StringIO(csv_text), infer_schema_length=0))
    cols = {c.upper(): c for c in df.columns}
    if "SYMBOL" not in cols or "BAND" not in cols:
        raise BhavParseError(f"price band file missing columns: {df.columns}")

    remarks = (
        pl.col(cols["REMARKS"]).cast(pl.Utf8).str.strip_chars()
        if "REMARKS" in cols
        else pl.lit(None, dtype=pl.Utf8)
    )
    series = (
        pl.col(cols["SERIES"]).cast(pl.Utf8).str.strip_chars().str.to_uppercase()
        if "SERIES" in cols
        else pl.lit("EQ")
    )
    return df.select(
        pl.col(cols["SYMBOL"]).cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
        series.alias("series"),
        pl.col(cols["BAND"]).cast(pl.Utf8).str.strip_chars().alias("price_band"),
        remarks.alias("band_remarks"),
    ).unique(subset=["symbol", "series"], keep="first")


def enrich(
    bhav: pl.DataFrame,
    delivery: pl.DataFrame | None = None,
    pricebands: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Left-join delivery and price-band data onto the canonical bhav frame.

    Left joins on purpose: delivery data does not exist before 2020 and a
    missing band file must never drop a price bar.
    """
    out = bhav
    if delivery is not None and not delivery.is_empty():
        out = out.join(delivery, on=["trade_date", "symbol", "series"], how="left")
    else:
        out = out.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("vwap"),
            pl.lit(None, dtype=pl.Int64).alias("deliv_qty"),
            pl.lit(None, dtype=pl.Float64).alias("deliv_pct"),
        )

    if pricebands is not None and not pricebands.is_empty():
        out = out.join(pricebands, on=["symbol", "series"], how="left")
    else:
        out = out.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("price_band"),
            pl.lit(None, dtype=pl.Utf8).alias("band_remarks"),
        )

    # Fall back to turnover/volume when the delivery file has no VWAP for a row.
    out = out.with_columns(
        pl.when(pl.col("vwap").is_not_null() & (pl.col("vwap") > 0))
        .then(pl.col("vwap"))
        .when((pl.col("volume") > 0) & pl.col("turnover").is_not_null())
        .then(pl.col("turnover") / pl.col("volume"))
        .otherwise(None)
        .alias("vwap")
    )
    return out.with_columns(_circuit_flag())


def _circuit_flag() -> pl.Expr:
    """Flag a bar whose close sits at a circuit edge.

    Two independent signals, because neither is complete on its own:
      * the band file's numeric percentage against the actual move, and
      * a degenerate bar where open == high == low == close, which is what a
        stock locked at a circuit for the whole session looks like.
    """
    band_pct = (
        pl.col("price_band")
        .cast(pl.Utf8)
        .str.extract(r"(\d+(?:\.\d+)?)", 1)
        .cast(pl.Float64, strict=False)
    )
    move_pct = (
        ((pl.col("c") - pl.col("prev_close")).abs() / pl.col("prev_close")) * 100.0
    )
    locked = (
        (pl.col("o") == pl.col("h")) & (pl.col("h") == pl.col("l")) & (pl.col("l") == pl.col("c"))
    )
    return (
        pl.when(band_pct.is_not_null() & pl.col("prev_close").is_not_null() & (pl.col("prev_close") > 0))
        .then((move_pct >= (band_pct - 0.05)) | locked)
        .otherwise(locked)
        .fill_null(False)
        .alias("circuit_flag")
    )
