"""Materialize gold.eod_adjusted = raw x cumulative factor.

Rebuild unit is the **ISIN**, not the day: one new ex-date rescales a symbol's
entire history, so a day-scoped rebuild would leave the past inconsistent with
the present. Gold is a pure function of bronze + the factor table, so it can be
dropped and rebuilt at any time without loss.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl

from ..config import Settings, get_settings
from ..db import connection, upsert_rows
from ..logging_setup import get_logger
from ..sources.security_master import GOLD_SERIES, TRADEABLE_SERIES, classify_series
from .cumulative import build_tr_factors, cumulative_factor_frame

log = get_logger(__name__)

GOLD_TABLE = "gold.eod_adjusted"

GOLD_COLUMNS = [
    "trade_date", "isin", "isin_traded", "symbol", "series",
    "o_adj", "h_adj", "l_adj", "c_adj", "v_adj",
    "o_tr", "h_tr", "l_tr", "c_tr",
    "c_raw", "cum_factor", "cum_factor_vol", "cum_factor_tr",
    "volume", "turnover", "vwap_adj", "deliv_pct",
    "in_universe", "tradeable", "series_flag", "adv_20",
    "circuit_flag", "price_band", "factor_asof",
]


def _fetch_bars(conn, canonical_isins: list[str]) -> pl.DataFrame:
    """Fetch bars for the given CANONICAL isins.

    Bars are looked up through ``silver.isin_link``, so a security whose ISIN
    changed at a face-value split returns its full history under one key. The
    as-traded identifier is kept as ``isin_traded`` for provenance.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.trade_date,
                   COALESCE(l.canonical_isin, b.isin) AS isin,
                   b.isin AS isin_traded,
                   b.symbol, b.series,
                   b.o, b.h, b.l, b.c, b.prev_close, b.vwap,
                   b.volume, b.turnover, b.deliv_pct,
                   b.circuit_flag, b.price_band,
                   sm.status AS status, sm.delisting_date
              FROM bronze.eod_bhav_raw b
              LEFT JOIN silver.isin_link l ON l.isin = b.isin
              LEFT JOIN silver.security_master sm
                     ON sm.isin = COALESCE(l.canonical_isin, b.isin)
             WHERE COALESCE(l.canonical_isin, b.isin) = ANY(%s)
             ORDER BY 2, b.series, b.trade_date
            """,
            (canonical_isins,),
        )
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None)


def _fetch_events(conn, isins: list[str], as_of: dt.datetime | None) -> dict[str, list[dict]]:
    """Events keyed by CANONICAL isin, matching how bars are fetched."""
    sql = """
        SELECT COALESCE(canonical_isin, isin) AS isin,
               ex_date, type, factor_price, factor_vol,
               div_amount, anchor_price, learned_at
          FROM silver.corp_action_event
         WHERE COALESCE(canonical_isin, isin) = ANY(%s)
           AND superseded_at IS NULL
           AND factor_state IN ('computed', 'not_applicable', 'skipped_s_gt_p')
    """
    params: list = [isins]
    if as_of is not None:
        # As-of reproducibility: only fold in what was known at that time.
        sql += " AND learned_at <= %s"
        params.append(as_of)
    sql += " ORDER BY 1, ex_date"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    by_isin: dict[str, list[dict]] = {}
    for r in rows:
        by_isin.setdefault(r["isin"], []).append(dict(r))
    return by_isin


def _universe_flags(df: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """Classify series, compute ADV, and set universe membership.

    ``in_universe`` is intentionally strict (EQ, not delisted as at the bar, and
    above the ADV floor). Everything else is still stored, just flagged out, so
    a downstream backtest can widen the universe without re-ingesting.
    """
    series_flag = (
        pl.col("series")
        .map_elements(classify_series, return_dtype=pl.Utf8)
        .alias("series_flag")
    )
    df = df.with_columns(series_flag)

    # 20-day rolling average turnover, per (isin, series).
    df = df.sort(["isin", "series", "trade_date"]).with_columns(
        pl.col("turnover")
        .cast(pl.Float64)
        .rolling_mean(window_size=settings.adv_window, min_samples=1)
        .over(["isin", "series"])
        .alias("adv_20")
    )

    # STRICTLY greater than, not >=. `delisting_date` is set to the LAST date the
    # security actually traded, so `>=` excluded that final bar -- which is the
    # exit bar for any position still open. A backtest that cannot see the last
    # traded price cannot close the position, which silently turns every delisting
    # into an unrealisable holding.
    delisted_by_date = pl.col("delisting_date").is_not_null() & (
        pl.col("trade_date") > pl.col("delisting_date")
    )
    # ETF / mutual-fund units (INF* ISINs) trade in series EQ and so cannot be
    # excluded on series alone. Their unit splits are absent from the equity
    # corporate-actions feed, so their adjusted series is not trustworthy.
    is_equity_isin = ~pl.col("isin").cast(pl.Utf8).str.to_uppercase().str.starts_with("INF")
    # Universe series come from config (default EQ + SME), so widening or
    # narrowing the tradeable set never needs a code change.
    tradeable = pl.col("series_flag").is_in(list(settings.universe_series)) & is_equity_isin
    liquid = (
        pl.lit(True)
        if settings.universe_min_adv <= 0
        else pl.col("adv_20").fill_null(0.0) >= settings.universe_min_adv
    )

    return df.with_columns(
        tradeable.alias("tradeable"),
        (tradeable & ~delisted_by_date & liquid).fill_null(False).alias("in_universe"),
    )


def _apply_factors(bars: pl.DataFrame, events: list[dict]) -> pl.DataFrame:
    """Multiply a single ISIN's bars by its cumulative factors."""
    trade_dates = bars["trade_date"].unique().sort().to_list()

    # Drop events whose ex-date is beyond the panel's right edge. NSE publishes
    # corporate actions in advance, and folding a FUTURE ex-date into today's
    # cum_factor would (a) make the latest adjusted close disagree with the
    # exchange-printed close, and (b) inject look-ahead bias -- history would be
    # rescaled using knowledge of an event that has not happened yet. Observed
    # live: TCC had a split with ex_date 2026-09-04 while the panel ended
    # 2026-08-28, which scaled its entire history by 0.2 prematurely.
    edge = max(trade_dates) if trade_dates else None
    usable = (
        [e for e in events if e.get("ex_date") is not None and e["ex_date"] <= edge]
        if edge is not None
        else []
    )

    ev = build_tr_factors(usable) if usable else []
    cf = cumulative_factor_frame(ev, trade_dates)

    out = bars.join(cf, on="trade_date", how="left").with_columns(
        pl.col("cum_factor").fill_null(1.0),
        pl.col("cum_factor_vol").fill_null(1.0),
        pl.col("cum_factor_tr").fill_null(1.0),
    )

    f = pl.col("cum_factor")
    fv = pl.col("cum_factor_vol")
    ftr = pl.col("cum_factor_tr")

    return out.with_columns(
        (pl.col("o").cast(pl.Float64) * f).alias("o_adj"),
        (pl.col("h").cast(pl.Float64) * f).alias("h_adj"),
        (pl.col("l").cast(pl.Float64) * f).alias("l_adj"),
        (pl.col("c").cast(pl.Float64) * f).alias("c_adj"),
        (pl.col("volume").cast(pl.Float64) * fv).alias("v_adj"),
        (pl.col("o").cast(pl.Float64) * ftr).alias("o_tr"),
        (pl.col("h").cast(pl.Float64) * ftr).alias("h_tr"),
        (pl.col("l").cast(pl.Float64) * ftr).alias("l_tr"),
        (pl.col("c").cast(pl.Float64) * ftr).alias("c_tr"),
        pl.col("c").cast(pl.Float64).alias("c_raw"),
        (pl.col("vwap").cast(pl.Float64) * f).alias("vwap_adj"),
    )


def materialize(
    isins: list[str] | None = None,
    as_of: dt.datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Rebuild gold rows for the given ISINs (all ISINs when None).

    Idempotent: the same inputs produce byte-identical rows, and the upsert only
    touches rows whose values actually changed.
    """
    s = settings or get_settings()
    stats = {"isins": 0, "rows": 0, "skipped_no_bars": 0}

    with connection(s) as conn:
        if isins is None:
            with conn.cursor() as cur:
                # Canonical isins: a split-renamed security must be rebuilt once
                # as one entity, not twice as two.
                cur.execute(
                    """
                    SELECT DISTINCT COALESCE(l.canonical_isin, b.isin) AS isin
                      FROM bronze.eod_bhav_raw b
                      LEFT JOIN silver.isin_link l ON l.isin = b.isin
                     ORDER BY 1
                    """
                )
                isins = [r["isin"] for r in cur.fetchall()]

        if not isins:
            return stats

        # Chunk so memory stays bounded on a full rebuild. The right chunk size
        # depends on HISTORY DEPTH, not just the ISIN count: 300 ISINs x 20 days
        # is 6k rows, but 300 ISINs x 1,600 days is ~500k rows held in polars at
        # once. Target roughly 60k bars per chunk.
        with conn.cursor() as cur:
            cur.execute("SELECT count(DISTINCT trade_date) AS d FROM bronze.eod_bhav_raw")
            depth = max(cur.fetchone()["d"] or 1, 1)
        CHUNK = max(25, min(300, 60_000 // depth))
        log.info("gold_chunk_size", isins_per_chunk=CHUNK, history_days=depth)
        for i in range(0, len(isins), CHUNK):
            chunk = isins[i : i + CHUNK]
            bars = _fetch_bars(conn, chunk)
            if bars.is_empty():
                stats["skipped_no_bars"] += len(chunk)
                continue

            events_by_isin = _fetch_events(conn, chunk, as_of)

            # Only equity-like series reach gold. Debt/GS stay in bronze only.
            bars = bars.with_columns(
                pl.col("series")
                .map_elements(classify_series, return_dtype=pl.Utf8)
                .alias("_sf")
            ).filter(pl.col("_sf").is_in(list(GOLD_SERIES))).drop("_sf")
            if bars.is_empty():
                continue

            pieces = []
            for isin in bars["isin"].unique().to_list():
                sub = bars.filter(pl.col("isin") == isin)
                pieces.append(_apply_factors(sub, events_by_isin.get(isin, [])))

            joined = pl.concat(pieces, how="vertical_relaxed")
            joined = _universe_flags(joined, s)

            rows = joined.select(
                [c for c in GOLD_COLUMNS if c in joined.columns]
            ).to_dicts()

            # factor_asof = latest learned_at folded into this ISIN's cum_factor.
            # Attached in Python rather than via polars: tz-aware datetimes from
            # psycopg do not round-trip cleanly through map_elements, and this is
            # a per-ISIN scalar lookup, not a vector operation.
            asof_map = {
                isin: max((e["learned_at"] for e in evs if e.get("learned_at")), default=None)
                for isin, evs in events_by_isin.items()
            }
            now = dt.datetime.now(dt.timezone.utc)
            for r in rows:
                r["factor_asof"] = asof_map.get(r["isin"])
                r["updated_at"] = now

            written = upsert_rows(
                conn,
                GOLD_TABLE,
                rows,
                conflict_cols=["trade_date", "isin", "series"],
                update_cols=[
                    c for c in GOLD_COLUMNS if c not in ("trade_date", "isin", "series")
                ]
                + ["updated_at"],
            )
            stats["isins"] += len(chunk)
            stats["rows"] += written
            log.info("gold_chunk", isins=len(chunk), rows=written)

    log.info("gold_materialized", **stats)
    return stats
