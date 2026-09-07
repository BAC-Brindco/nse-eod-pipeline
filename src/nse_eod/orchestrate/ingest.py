"""Ingest steps: bronze bars, corp actions -> silver events, security master, calendar."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import polars as pl

from ..config import Settings, get_settings
from ..db import REVIEW_QUEUE_CONFLICT, connection, insert_ignore, row_hash, upsert_rows
from ..logging_setup import get_logger
from ..parsers.corp_action_text import (
    PARSER_VERSION,
    PRICE_ADJUSTING,
    parse_purpose,
    review_severity,
)
from ..sources import holidays as hol
from ..sources.bhavcopy import BhavcopyUnavailable, fetch_eod_frame
from ..sources.corp_actions import fetch_corp_actions, to_bronze_rows
from ..sources.http import NSESession
from ..sources.security_master import (
    STALE_DAYS,
    fetch_equity_list,
    merge_symbol_history,
)

log = get_logger(__name__)

BRONZE_BHAV_COLS = [
    "trade_date", "isin", "symbol", "series",
    "o", "h", "l", "c", "last_price", "prev_close", "vwap",
    "volume", "turnover", "trades", "deliv_qty", "deliv_pct",
    "price_band", "band_remarks", "circuit_flag", "instrument_type",
    "source_format", "source_file", "row_hash",
]


# --------------------------------------------------------------------- bronze
def ingest_eod(
    trade_date: dt.date,
    session: NSESession,
    settings: Settings | None = None,
) -> dict:
    """Fetch, validate and upsert one day of bars into bronze.

    Returns a dict with ``rows``, ``restated``, ``content_hash``, ``source_format``.
    Re-running an unchanged day writes nothing (the upsert is gated on row_hash).
    """
    s = settings or get_settings()
    df, fmt, src_file, content_hash = fetch_eod_frame(trade_date, session, s)

    rows: list[dict] = []
    for r in df.to_dicts():
        payload = (
            r.get("o"), r.get("h"), r.get("l"), r.get("c"),
            r.get("last_price"), r.get("prev_close"), r.get("volume"),
            r.get("turnover"), r.get("trades"), r.get("deliv_qty"),
        )
        rows.append(
            {
                "trade_date": r["trade_date"],
                "isin": r["isin"],
                "symbol": r["symbol"],
                "series": r["series"],
                "o": r.get("o"), "h": r.get("h"), "l": r.get("l"), "c": r.get("c"),
                "last_price": r.get("last_price"),
                "prev_close": r.get("prev_close"),
                "vwap": r.get("vwap"),
                "volume": r.get("volume"),
                "turnover": r.get("turnover"),
                "trades": r.get("trades"),
                "deliv_qty": r.get("deliv_qty"),
                "deliv_pct": r.get("deliv_pct"),
                "price_band": r.get("price_band"),
                "band_remarks": r.get("band_remarks"),
                "circuit_flag": r.get("circuit_flag"),
                "instrument_type": r.get("instrument_type"),
                "source_format": fmt,
                "source_file": src_file,
                "row_hash": row_hash(payload),
            }
        )

    with connection(s) as conn:
        # Count how many existing rows would actually change: that is a
        # restatement by NSE, and it is the one case where re-running a day is
        # legitimately not a no-op.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS n
                  FROM bronze.eod_bhav_raw b
                  JOIN (SELECT unnest(%s::text[]) AS isin,
                               unnest(%s::text[]) AS series,
                               unnest(%s::text[]) AS row_hash) x
                    ON x.isin = b.isin AND x.series = b.series
                 WHERE b.trade_date = %s
                   AND b.row_hash <> x.row_hash
                """,
                (
                    [r["isin"] for r in rows],
                    [r["series"] for r in rows],
                    [r["row_hash"] for r in rows],
                    trade_date,
                ),
            )
            restated = cur.fetchone()["n"]

        written = upsert_rows(
            conn,
            "bronze.eod_bhav_raw",
            rows,
            conflict_cols=["trade_date", "isin", "series"],
            update_cols=[c for c in BRONZE_BHAV_COLS if c not in ("trade_date", "isin", "series")],
            # Only touch the row when the content genuinely differs.
            where_clause="bronze.eod_bhav_raw.row_hash <> EXCLUDED.row_hash",
        )

        # A parsed bhavcopy is the strongest evidence the market was open.
        upsert_rows(
            conn,
            "bronze.trading_calendar",
            [hol.observed_row(trade_date)],
            conflict_cols=["cal_date"],
            update_cols=["is_trading_day", "reason", "segment", "source", "updated_at"],
        )

    log.info(
        "bronze_ingested",
        date=str(trade_date),
        rows=len(rows),
        upserted=written,
        restated=restated,
        format=fmt,
    )
    return {
        "rows": len(rows),
        "upserted": written,
        "restated": restated,
        "content_hash": content_hash,
        "source_format": fmt,
        "frame": df,
    }


# ------------------------------------------------------------ corp actions
def _dec(x) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def ingest_corp_actions(
    start: dt.date,
    end: dt.date,
    session: NSESession,
    settings: Settings | None = None,
    scrape_date: dt.date | None = None,
) -> dict:
    """Fetch corp actions, append to bronze, parse into silver events + review queue."""
    s = settings or get_settings()
    scrape_date = scrape_date or dt.date.today()

    api_rows = fetch_corp_actions(start, end, session, s)
    bronze_rows = to_bronze_rows(api_rows, scrape_date)

    stats = {
        "ca_rows_fetched": len(api_rows),
        "ca_rows_ingested": 0,
        "events_parsed": 0,
        "flagged_for_review": 0,
        "price_adjusting": 0,
    }
    if not bronze_rows:
        return stats

    with connection(s) as conn:
        insert_ignore(conn, "bronze.corp_action_raw", bronze_rows, ["payload_hash"])

        # Read back ids for the hashes we just handled (new or pre-existing), so
        # events always reference a real bronze row.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ca_raw_id, payload_hash, isin, symbol, ex_date, record_date,
                       face_value, purpose_text
                  FROM bronze.corp_action_raw
                 WHERE payload_hash = ANY(%s)
                """,
                ([r["payload_hash"] for r in bronze_rows],),
            )
            raws = cur.fetchall()
        stats["ca_rows_ingested"] = len(raws)

        events: list[dict] = []
        reviews: list[dict] = []

        for raw in raws:
            isin = raw["isin"]
            ex_date = raw["ex_date"]
            purpose = raw["purpose_text"]

            if not isin or not ex_date:
                # Cannot key an event without both. Never guess an ex-date.
                reviews.append(
                    {
                        "ca_raw_id": raw["ca_raw_id"],
                        "isin": isin,
                        "symbol": raw["symbol"],
                        "ex_date": ex_date,
                        "raw_purpose": purpose,
                        "reason": "unparsed_pattern"
                        if isin
                        else "ambiguous_ratio",
                        "parser_version": PARSER_VERSION,
                        "suggested_type": None,
                        "suggested_json": json.dumps(
                            {"missing": "isin" if not isin else "ex_date"}
                        ),
                        "severity": "high",
                    }
                )
                continue

            result = parse_purpose(purpose, raw["face_value"])

            for comp in result.components:
                events.append(
                    {
                        "event_uid": comp.event_uid(isin, ex_date),
                        "isin": isin,
                        "symbol": raw["symbol"],
                        "ex_date": ex_date,
                        "record_date": raw["record_date"],
                        "type": comp.type,
                        "ratio_num": comp.ratio_num,
                        "ratio_den": comp.ratio_den,
                        "sub_price": comp.sub_price,
                        "premium": comp.premium,
                        "face_value": comp.face_value or _dec(raw["face_value"]),
                        "div_amount": comp.div_amount,
                        "is_special": comp.is_special,
                        "parsed_ok": comp.parsed_ok,
                        "parser_version": PARSER_VERSION,
                        "confidence": comp.confidence,
                        "component_ix": comp.component_ix,
                        "raw_purpose": comp.raw_text,
                        "ca_raw_id": raw["ca_raw_id"],
                        "factor_state": "pending",
                    }
                )
                if comp.type in PRICE_ADJUSTING and comp.parsed_ok:
                    stats["price_adjusting"] += 1

                if comp.needs_review:
                    reviews.append(
                        {
                            "ca_raw_id": raw["ca_raw_id"],
                            "isin": isin,
                            "symbol": raw["symbol"],
                            "ex_date": ex_date,
                            "raw_purpose": comp.raw_text,
                            "reason": comp.review_reason,
                            "parser_version": PARSER_VERSION,
                            "suggested_type": comp.type,
                            "suggested_json": json.dumps(
                                {
                                    "type": comp.type,
                                    "ratio_num": str(comp.ratio_num) if comp.ratio_num else None,
                                    "ratio_den": str(comp.ratio_den) if comp.ratio_den else None,
                                    "premium": str(comp.premium) if comp.premium else None,
                                    "div_amount": str(comp.div_amount) if comp.div_amount else None,
                                    "confidence": comp.confidence,
                                }
                            ),
                            "severity": review_severity(comp),
                        }
                    )

        if events:
            # Do NOT overwrite factor_state on conflict: an event already
            # computed must not be reset to 'pending' by a daily re-scrape.
            upsert_rows(
                conn,
                "silver.corp_action_event",
                events,
                conflict_cols=["event_uid"],
                update_cols=[
                    "symbol", "record_date", "ratio_num", "ratio_den", "sub_price",
                    "premium", "face_value", "div_amount", "is_special",
                    "parsed_ok", "parser_version", "confidence", "raw_purpose",
                    "ca_raw_id", "updated_at",
                ],
            )
            stats["events_parsed"] = len(events)

        if reviews:
            # Deduplicate within the batch on the queue's natural key.
            seen = set()
            deduped = []
            for r in reviews:
                k = (r["ca_raw_id"], r["raw_purpose"])
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(r)
            insert_ignore(
                conn,
                "silver.corp_action_review_queue",
                deduped,
                ["ca_raw_id", "raw_purpose"],
                conflict_target=REVIEW_QUEUE_CONFLICT,
            )
            stats["flagged_for_review"] = len(deduped)

    log.info("corp_actions_ingested", **stats)
    return stats


def reparse_corp_actions(
    settings: Settings | None = None,
    only_unparsed: bool = False,
    batch: int = 2000,
) -> dict:
    """Re-run the current parser over ALL stored raw announcements.

    Essential, not optional: a parser improvement is worthless if it only applies
    to announcements ingested *after* the fix. Bronze is immutable and complete,
    so silver can always be rebuilt from it.

    Concretely, the v1.1.0 parser fixes reached history only through this path:
      * ``Bonus- 1:2`` (AJANTPHARM 2022-06-22) -- punctuation between the label
        and the ratio; previously unparsed, leaving that symbol's whole pre-2022
        history unadjusted by a third.
      * ``Rights Issue 30:37@ Premium Rs 53/-`` (ASIANTILES) and BHAGCHEM --
        the word "Issue" between label and ratio.

    Events that no longer appear in a re-parse are marked ``superseded_at``
    rather than deleted, so the audit trail survives. ``factor_state`` is reset
    to ``pending`` only when the parse RESULT actually changed, so an unchanged
    re-parse leaves computed factors alone and stays idempotent.
    """
    s = settings or get_settings()
    stats = {
        "raw_examined": 0,
        "events_upserted": 0,
        "events_superseded": 0,
        "events_changed": 0,
        "newly_parsed": 0,
        "reviews_resolved": 0,
        "reviews_added": 0,
    }

    with connection(s) as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT r.ca_raw_id, r.isin, r.symbol, r.ex_date, r.record_date,
                       r.face_value, r.purpose_text
                  FROM bronze.corp_action_raw r
                 WHERE r.isin IS NOT NULL AND r.ex_date IS NOT NULL
            """
            if only_unparsed:
                # Only rows that currently have at least one unparsed component.
                sql += """
                   AND EXISTS (
                        SELECT 1 FROM silver.corp_action_event e
                         WHERE e.ca_raw_id = r.ca_raw_id
                           AND e.superseded_at IS NULL
                           AND NOT e.parsed_ok
                       )
                """
            sql += " ORDER BY r.ca_raw_id"
            cur.execute(sql)
            raws = cur.fetchall()

        stats["raw_examined"] = len(raws)
        if not raws:
            return stats

        # Existing state, so a change can be detected rather than assumed.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ca_raw_id, event_uid, type, parsed_ok, factor_state,
                       ratio_num, ratio_den, sub_price, div_amount
                  FROM silver.corp_action_event
                 WHERE superseded_at IS NULL AND ca_raw_id = ANY(%s)
                """,
                ([r["ca_raw_id"] for r in raws],),
            )
            existing: dict[int, dict[str, dict]] = {}
            for row in cur.fetchall():
                existing.setdefault(row["ca_raw_id"], {})[row["event_uid"]] = dict(row)

        fresh_events: list[dict] = []
        fresh_reviews: list[dict] = []
        to_supersede: list[str] = []
        now_parsed_uids: list[str] = []

        for raw in raws:
            isin, ex_date = raw["isin"], raw["ex_date"]
            result = parse_purpose(raw["purpose_text"], raw["face_value"])
            prior = existing.get(raw["ca_raw_id"], {})
            fresh_uids: set[str] = set()

            for comp in result.components:
                uid = comp.event_uid(isin, ex_date)
                fresh_uids.add(uid)
                was = prior.get(uid)

                # Reset the factor only when the parse result actually moved.
                changed = was is None or (
                    was["type"] != comp.type
                    or bool(was["parsed_ok"]) != comp.parsed_ok
                    or _dec(was["ratio_num"]) != comp.ratio_num
                    or _dec(was["ratio_den"]) != comp.ratio_den
                    or _dec(was["sub_price"]) != comp.sub_price
                    or _dec(was["div_amount"]) != comp.div_amount
                )
                if changed:
                    stats["events_changed"] += 1
                if was is not None and not was["parsed_ok"] and comp.parsed_ok:
                    stats["newly_parsed"] += 1
                    now_parsed_uids.append(uid)

                fresh_events.append(
                    {
                        "event_uid": uid,
                        "isin": isin,
                        "symbol": raw["symbol"],
                        "ex_date": ex_date,
                        "record_date": raw["record_date"],
                        "type": comp.type,
                        "ratio_num": comp.ratio_num,
                        "ratio_den": comp.ratio_den,
                        "sub_price": comp.sub_price,
                        "premium": comp.premium,
                        "face_value": comp.face_value or _dec(raw["face_value"]),
                        "div_amount": comp.div_amount,
                        "is_special": comp.is_special,
                        "parsed_ok": comp.parsed_ok,
                        "parser_version": PARSER_VERSION,
                        "confidence": comp.confidence,
                        "component_ix": comp.component_ix,
                        "raw_purpose": comp.raw_text,
                        "ca_raw_id": raw["ca_raw_id"],
                        "factor_state": "pending"
                        if changed
                        else (was["factor_state"] if was else "pending"),
                    }
                )

                if comp.needs_review:
                    fresh_reviews.append(
                        {
                            "ca_raw_id": raw["ca_raw_id"],
                            "isin": isin,
                            "symbol": raw["symbol"],
                            "ex_date": ex_date,
                            "raw_purpose": comp.raw_text,
                            "reason": comp.review_reason,
                            "parser_version": PARSER_VERSION,
                            "suggested_type": comp.type,
                            "suggested_json": json.dumps(
                                {
                                    "type": comp.type,
                                    "ratio_num": str(comp.ratio_num) if comp.ratio_num else None,
                                    "ratio_den": str(comp.ratio_den) if comp.ratio_den else None,
                                    "premium": str(comp.premium) if comp.premium else None,
                                    "div_amount": str(comp.div_amount) if comp.div_amount else None,
                                    "confidence": comp.confidence,
                                }
                            ),
                            "severity": review_severity(comp),
                        }
                    )

            # Anything the old parser produced that the new one does not.
            to_supersede.extend(uid for uid in prior if uid not in fresh_uids)

        # ---- write ---------------------------------------------------------
        for i in range(0, len(fresh_events), batch):
            stats["events_upserted"] += upsert_rows(
                conn,
                "silver.corp_action_event",
                fresh_events[i : i + batch],
                conflict_cols=["event_uid"],
                update_cols=[
                    "symbol", "record_date", "type", "ratio_num", "ratio_den", "sub_price",
                    "premium", "face_value", "div_amount", "is_special", "parsed_ok",
                    "parser_version", "confidence", "raw_purpose", "ca_raw_id",
                    "factor_state", "updated_at",
                ],
            )

        if to_supersede:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE silver.corp_action_event
                       SET superseded_at = now(), updated_at = now()
                     WHERE event_uid = ANY(%s) AND superseded_at IS NULL
                    """,
                    (to_supersede,),
                )
                stats["events_superseded"] = cur.rowcount or 0

        if fresh_reviews:
            seen, deduped = set(), []
            for r in fresh_reviews:
                k = (r["ca_raw_id"], r["raw_purpose"])
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(r)
            # UPSERT, not insert-ignore: an OPEN review row must reflect the
            # current parser's opinion. Britannia's debenture bonus, for
            # instance, moved from suggested_type BONUS to
            # SCHEME_OF_ARRANGEMENT/needs_external_price -- a reviewer acting on
            # the stale suggestion would reach the wrong conclusion.
            stats["reviews_added"] = upsert_rows(
                conn,
                "silver.corp_action_review_queue",
                deduped,
                conflict_cols=["ca_raw_id", "raw_purpose"],
                update_cols=[
                    "reason", "parser_version", "suggested_type",
                    "suggested_json", "severity",
                ],
                conflict_target=REVIEW_QUEUE_CONFLICT,
                # Never overwrite a decision a human already made.
                where_clause="silver.corp_action_review_queue.status = 'open'",
            )

        # Close out review items whose component now parses cleanly, so the
        # queue reflects the CURRENT parser rather than accumulating history.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE silver.corp_action_review_queue q
                   SET status = 'resolved',
                       resolution_note = 'auto-resolved: parser ' || %s || ' now parses this',
                       resolved_by = 'reparse',
                       resolved_at = now()
                 WHERE q.status = 'open'
                   AND EXISTS (
                        SELECT 1 FROM silver.corp_action_event e
                         WHERE e.ca_raw_id = q.ca_raw_id
                           AND e.raw_purpose = q.raw_purpose
                           AND e.superseded_at IS NULL
                           AND e.parsed_ok
                       )
                """,
                (PARSER_VERSION,),
            )
            stats["reviews_resolved"] = cur.rowcount or 0

    log.info("corp_actions_reparsed", **stats)
    return stats


# ------------------------------------------------------------ security master
def ingest_security_master(
    session: NSESession | None = None,
    settings: Settings | None = None,
    equity_list: pl.DataFrame | None = None,
) -> dict:
    """Refresh the security master from EQUITY_L.csv plus everything in bronze.

    EQUITY_L lists only currently-listed securities, so bronze is used as the
    second source: anything ever seen in a bhavcopy stays in the master with a
    frozen ``delisting_date``. Dropping those rows would inject survivorship
    bias into every downstream backtest.
    """
    s = settings or get_settings()
    if equity_list is None:
        if session is None:
            raise ValueError("need either a session or an equity_list frame")
        equity_list = fetch_equity_list(session)

    stats = {"from_equity_l": len(equity_list), "upserted": 0, "marked_delisted": 0}

    with connection(s) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT isin,
                       min(trade_date) AS first_seen,
                       max(trade_date) AS last_seen,
                       (array_agg(symbol ORDER BY trade_date DESC))[1] AS last_symbol,
                       (array_agg(series ORDER BY trade_date DESC))[1] AS last_series
                  FROM bronze.eod_bhav_raw
                 GROUP BY isin
                """
            )
            seen = {r["isin"]: r for r in cur.fetchall()}

            cur.execute("SELECT isin, symbol_history FROM silver.security_master")
            existing_hist = {r["isin"]: r["symbol_history"] for r in cur.fetchall()}

        listed = {r["isin"]: r for r in equity_list.to_dicts()}
        all_isins = set(listed) | set(seen)

        max_bar = max((v["last_seen"] for v in seen.values()), default=None)

        rows = []
        for isin in sorted(all_isins):
            L = listed.get(isin)
            S = seen.get(isin)
            symbol = (L or {}).get("symbol") or (S or {}).get("last_symbol")
            if not symbol:
                continue

            hist = existing_hist.get(isin)
            if S and S.get("last_symbol"):
                hist, _ = merge_symbol_history(hist, S["last_symbol"], S["last_seen"])
            if L and L.get("symbol") and S:
                hist, _ = merge_symbol_history(hist, L["symbol"], S["last_seen"])

            if isin in listed:
                status, delisting_date = "active", None
            elif S and max_bar and (max_bar - S["last_seen"]).days > STALE_DAYS:
                status, delisting_date = "delisted", S["last_seen"]
            else:
                status, delisting_date = "suspended", None
                if S and max_bar and S["last_seen"] == max_bar:
                    status = "active"

            if status == "delisted":
                stats["marked_delisted"] += 1

            rows.append(
                {
                    "isin": isin,
                    "symbol": symbol,
                    "name": (L or {}).get("name"),
                    "series": (L or {}).get("series") or (S or {}).get("last_series"),
                    "face_value": (L or {}).get("face_value"),
                    "listing_date": (L or {}).get("listing_date"),
                    "delisting_date": delisting_date,
                    "status": status,
                    "symbol_history": json.dumps(hist or []),
                    "first_seen": (S or {}).get("first_seen"),
                    "last_seen": (S or {}).get("last_seen"),
                }
            )

        stats["upserted"] = upsert_rows(
            conn,
            "silver.security_master",
            rows,
            conflict_cols=["isin"],
            update_cols=[
                "symbol", "name", "series", "face_value", "listing_date",
                "delisting_date", "status", "symbol_history",
                "first_seen", "last_seen", "updated_at",
            ],
        )

    log.info("security_master_ingested", **stats)
    return stats


# ---------------------------------------------------------------- calendar
def ingest_calendar(
    start: dt.date,
    end: dt.date,
    session: NSESession | None = None,
    settings: Settings | None = None,
) -> dict:
    """Populate the trading calendar for a range.

    Applied in increasing authority: weekdays (provisional trading), weekends,
    then the published holiday master. ``observed_bhavcopy`` rows written by
    ``ingest_eod`` outrank all of these and are never overwritten here.
    """
    s = settings or get_settings()
    rows = hol.weekday_rows(start, end) + hol.weekend_rows(start, end)
    # NOTE: weekend_rows marks Saturdays non-trading at the LOWEST authority, so
    # an observed_bhavcopy row from a real Saturday session overrides it.
    holidays = hol.fetch_holidays(session, settings=s) if session else []
    rows += [h for h in holidays if start <= h["cal_date"] <= end]

    with connection(s) as conn:
        upsert_rows(
            conn,
            "bronze.trading_calendar",
            rows,
            conflict_cols=["cal_date"],
            update_cols=["is_trading_day", "reason", "segment", "source", "updated_at"],
            # Never downgrade an observed trading day: NSE holds special
            # sessions (Muhurat, budget days) that the holiday master
            # mislabels, and a false "closed" would skip a real day forever.
            where_clause="bronze.trading_calendar.source <> 'observed_bhavcopy'",
        )

    log.info(
        "calendar_ingested",
        start=str(start),
        end=str(end),
        rows=len(rows),
        holidays=len(holidays),
    )
    return {"rows": len(rows), "holidays": len(holidays)}


def derive_calendar_from_observation(settings: Settings | None = None) -> dict:
    """Fill the historical calendar from what the data itself proves.

    NSE's ``holiday-master`` endpoint returns only the CURRENT calendar year, so
    a 2020-2025 backfill leaves 80+ weekdays with no calendar entry at all -- and
    an audit then cannot tell a genuine holiday from a missing ingest.

    Observation settles it without guessing:
      * a weekday WITH bars           -> trading day  (source observed_bhavcopy)
      * a weekday inside the ingested
        span with NO bars, whose
        ingest was attempted and 404'd -> holiday     (source observed_bhavcopy)

    The second rule relies on the watermark: a date recorded as ``skipped``
    means the fetch ran and NSE published nothing, which is exactly what a
    holiday looks like. A date never attempted is left absent, so a gap in
    coverage stays visible rather than being relabelled as a holiday.
    """
    s = settings or get_settings()
    rows: list[dict] = []

    with connection(s) as conn:
        with conn.cursor() as cur:
            # Every date we actually have bars for is a proven trading day.
            cur.execute("SELECT DISTINCT trade_date FROM bronze.eod_bhav_raw ORDER BY 1")
            traded = [r["trade_date"] for r in cur.fetchall()]

            # Attempted-and-empty weekdays: non-trading, but ONLY on evidence that
            # could actually distinguish a holiday from a file that was not published
            # yet.
            #
            # THE BUG THIS GUARDS, OBSERVED LIVE ON 2026-09-04
            # An index backfill ran at 14:49 IST -- market still open, hours before
            # NSE publishes the bhavcopy. The ingest attempt 404ed and was
            # watermarked 'skipped', so this query concluded "no bhavcopy published
            # (holiday, observed)" and wrote 2026-09-04 into the calendar as a
            # non-trading day.
            #
            # NSE had in fact traded that day. The next scheduled run-daily then
            # consulted the poisoned calendar, skipped the session, and -- because a
            # day marked holiday is never revisited -- would have left it missing
            # permanently. A whole trading session silently absent, with every run
            # reporting success.
            #
            # The flaw was treating ABSENCE OF A FILE as evidence of a holiday with
            # no notion of whether the file could exist yet. That is the same
            # distinction the NSE probe makes on purpose: 404 is a TIMING answer,
            # not a refusal.
            #
            # So two conditions, and both are necessary:
            #   cal_date < current_date      never judge a session that may still be
            #                                mid-publication
            #   completed_at::date >         the attempt must have been made on a
            #     target_date                LATER day than the session, so
            #                                publication had every chance. This is
            #                                what excludes the 2026-09-04 case,
            #                                where the attempt and the session were
            #                                the same day.
            cur.execute(
                """
                SELECT w.target_date
                  FROM ops.run_watermark w
                 WHERE w.command = 'ingest-eod'
                   AND w.status = 'skipped'
                   AND extract(isodow FROM w.target_date) < 6
                   AND w.target_date < current_date
                   AND (w.completed_at AT TIME ZONE 'Asia/Kolkata')::date > w.target_date
                   AND NOT EXISTS (
                        SELECT 1 FROM bronze.eod_bhav_raw b
                         WHERE b.trade_date = w.target_date
                       )
                 ORDER BY 1
                """
            )
            empty = [r["target_date"] for r in cur.fetchall()]

        for d in traded:
            rows.append(hol.observed_row(d))
        for d in empty:
            rows.append(
                {
                    "cal_date": d,
                    "is_trading_day": False,
                    "reason": "no bhavcopy published (holiday, observed)",
                    "segment": hol.SEGMENT,
                    "source": hol.SRC_OBSERVED,
                }
            )

        if rows:
            upsert_rows(
                conn,
                "bronze.trading_calendar",
                rows,
                conflict_cols=["cal_date"],
                update_cols=["is_trading_day", "reason", "segment", "source", "updated_at"],
            )

    stats = {"trading_days": len(traded), "observed_holidays": len(empty)}
    log.info("calendar_derived", **stats)
    return stats


def is_trading_day(d: dt.date, settings: Settings | None = None) -> tuple[bool, str]:
    """Calendar gate. Returns (is_trading, reason).

    Only SUNDAY is a hard non-trading day. Saturday is attempted because NSE
    holds occasional Saturday sessions (Budget day, special live sessions) and
    publishes a real bhavcopy for them; a 404 then settles it without alarm.
    """
    if hol.never_trades(d):
        return False, "sunday"
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT is_trading_day, reason, source FROM bronze.trading_calendar WHERE cal_date = %s",
            (d,),
        )
        row = cur.fetchone()
    if row is None:
        # Unknown weekday: assume it trades. A missing calendar entry must not
        # silently skip a real trading day; the bhavcopy 404 will settle it.
        return True, "unknown (assumed trading)"

    # A WEEKEND day marked non-trading by the blanket weekend rule is not
    # evidence. NSE runs weekend sessions in both directions:
    #   Saturdays: 2020-02-01, 2020-11-14 (Muhurat), 2024-01-20, 2024-03-02,
    #              2024-05-18, 2025-02-01
    #   Sunday:    2026-02-01 (Union Budget) -- 180 KB bhavcopy
    # Trusting the weekend rule would skip them in the live daily path even after
    # the backfill was taught to find them. Only the published holiday master or
    # an observed bhavcopy may rule a weekend day out.
    if (
        hol.is_weekend(d)
        and not row["is_trading_day"]
        and row["source"] == hol.SRC_WEEKEND
    ):
        return True, "weekend (weekend rule is not authoritative; will try the bhavcopy)"

    return bool(row["is_trading_day"]), row["reason"] or ""
