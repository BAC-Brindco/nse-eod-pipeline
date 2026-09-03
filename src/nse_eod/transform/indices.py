"""Persist index closes. Idempotent per (trade_date, index_key).

Re-ingesting a date REPLACES its rows rather than appending, exactly like the equity
path: NSE occasionally restates a file, and two generations of the same session would
make every downstream percentile wrong in a way nothing would flag.
"""

from __future__ import annotations

import datetime as dt

from ..config import Settings, get_settings
from ..db import connection
from ..logging_setup import get_logger

log = get_logger(__name__)


def upsert(
    bronze_rows: list[dict], silver_rows: list[dict], settings: Settings | None = None
) -> dict:
    s = settings or get_settings()
    if not bronze_rows:
        return {"bronze": 0, "silver": 0}

    with connection(s) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO bronze.index_close_raw (
                trade_date, index_name, open_txt, high_txt, low_txt, close_txt,
                points_chg, pct_chg, volume_txt, turnover_txt, pe_txt, pb_txt,
                div_yield_txt, source_file, row_hash
            ) VALUES (
                %(trade_date)s, %(index_name)s, %(open_txt)s, %(high_txt)s,
                %(low_txt)s, %(close_txt)s, %(points_chg)s, %(pct_chg)s,
                %(volume_txt)s, %(turnover_txt)s, %(pe_txt)s, %(pb_txt)s,
                %(div_yield_txt)s, %(source_file)s, %(row_hash)s
            )
            ON CONFLICT (trade_date, index_name) DO UPDATE SET
                open_txt = EXCLUDED.open_txt,
                high_txt = EXCLUDED.high_txt,
                low_txt = EXCLUDED.low_txt,
                close_txt = EXCLUDED.close_txt,
                points_chg = EXCLUDED.points_chg,
                pct_chg = EXCLUDED.pct_chg,
                volume_txt = EXCLUDED.volume_txt,
                turnover_txt = EXCLUDED.turnover_txt,
                pe_txt = EXCLUDED.pe_txt,
                pb_txt = EXCLUDED.pb_txt,
                div_yield_txt = EXCLUDED.div_yield_txt,
                source_file = EXCLUDED.source_file,
                row_hash = EXCLUDED.row_hash,
                ingested_at = now()
            """,
            [{k: v for k, v in r.items() if k != "index_key"} for r in bronze_rows],
        )
        n_bronze = len(bronze_rows)

        cur.executemany(
            """
            INSERT INTO silver.index_daily (
                trade_date, index_key, index_name, open, high, low, close,
                points_chg, pct_chg, volume, turnover_cr, pe, pb, div_yield
            ) VALUES (
                %(trade_date)s, %(index_key)s, %(index_name)s, %(open)s, %(high)s,
                %(low)s, %(close)s, %(points_chg)s, %(pct_chg)s, %(volume)s,
                %(turnover_cr)s, %(pe)s, %(pb)s, %(div_yield)s
            )
            ON CONFLICT (trade_date, index_key) DO UPDATE SET
                index_name = EXCLUDED.index_name,
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                points_chg = EXCLUDED.points_chg,
                pct_chg = EXCLUDED.pct_chg,
                volume = EXCLUDED.volume,
                turnover_cr = EXCLUDED.turnover_cr,
                pe = EXCLUDED.pe,
                pb = EXCLUDED.pb,
                div_yield = EXCLUDED.div_yield,
                updated_at = now()
            """,
            silver_rows,
        )
        n_silver = len(silver_rows)

    return {"bronze": n_bronze, "silver": n_silver}


def ingest_range(
    start: dt.date,
    end: dt.date,
    settings: Settings | None = None,
    skip_existing: bool = True,
) -> dict:
    """Walk a date range, fetching each day's index file.

    `skip_existing` makes the backfill resumable: an interrupted run picks up where it
    stopped instead of re-fetching seven years of archives.
    """
    from ..sources.http import NSESession
    from ..sources import indices as src

    s = settings or get_settings()
    have: set[dt.date] = set()
    if skip_existing:
        from ..db import fetch_all

        # NOTE: this repo's fetch_all takes (query, params) only -- it has no
        # `settings` kwarg (the momentum tracker's does, which is what tripped this).
        have = {
            r["trade_date"]
            for r in fetch_all(
                "SELECT DISTINCT trade_date FROM silver.index_daily "
                "WHERE trade_date BETWEEN %s AND %s",
                (start, end),
            )
        }

    # Weekend dates the exchange actually traded, per the pipeline's own calendar.
    from ..db import fetch_all as _fa

    weekend_sessions = {
        r["cal_date"]
        for r in _fa(
            "SELECT cal_date FROM bronze.trading_calendar "
            "WHERE is_trading_day AND cal_date BETWEEN %s AND %s "
            "AND extract(isodow FROM cal_date) >= 6",
            (start, end),
        )
    }
    if weekend_sessions:
        log.info("index_weekend_sessions_will_be_probed", n=len(weekend_sessions))

    stats = {"days_fetched": 0, "days_absent": 0, "days_skipped": 0, "rows": 0, "errors": 0}
    with NSESession(s) as session:
        d = start
        while d <= end:
            if d in have:
                stats["days_skipped"] += 1
                d += dt.timedelta(days=1)
                continue
            # Skip weekends to save ~700 pointless requests -- but ask the CALENDAR,
            # not the weekday number.
            #
            # This pipeline has already lost 8 sessions once to a weekday assumption:
            # NSE runs occasional Saturday/Sunday equity sessions (budget days,
            # muhurat, DR drills) and a `weekday() >= 5` rule dropped every one of
            # them. Measured 2026-09-03: all 8 such sessions between 2020 and 2026
            # exist in the equity panel and have NO index close file, so skipping them
            # is correct TODAY -- but if NSE ever publishes one, deferring to
            # trading_calendar means we probe it instead of silently skipping.
            if d.weekday() >= 5 and d not in weekend_sessions:
                stats["days_absent"] += 1
                d += dt.timedelta(days=1)
                continue
            try:
                got = src.fetch_and_parse(d, session, s)
                if got is None:
                    stats["days_absent"] += 1
                else:
                    bronze_rows, silver_rows = got
                    res = upsert(bronze_rows, silver_rows, s)
                    stats["days_fetched"] += 1
                    stats["rows"] += res["silver"]
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("index_ingest_failed", date=str(d), error=str(exc)[:200])
            d += dt.timedelta(days=1)

    log.info("index_ingest_done", start=str(start), end=str(end), **stats)
    return stats
