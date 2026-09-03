"""Resolve the anchor price P and compute factors for stored events.

P is the close on the last trading day **strictly before** ex_date, read from
bronze. Events whose P is not yet available (newly listed, suspended, or an
ex_date announced ahead of the bar existing) are parked at ``needs_anchor`` and
retried on every later run -- never silently defaulted to 1.0, because a missing
factor is indistinguishable downstream from a genuine no-op.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ..db import connection, execute
from ..logging_setup import get_logger
from ..parsers.corp_action_text import (
    DIVIDEND_TYPES,
    NEEDS_EXTERNAL,
    PRICE_ADJUSTING,
)
from .factors import (
    COMPUTED,
    NEEDS_ANCHOR,
    NOT_APPLICABLE,
    UNRESOLVED,
    FactorError,
    compute_factor,
)

log = get_logger(__name__)

# States worth revisiting on a later run.
RETRY_STATES = ("pending", NEEDS_ANCHOR)


def _anchor_sql() -> str:
    """Latest close strictly before ex_date, preferring the EQ bar.

    Joins through ``silver.isin_link`` on the CANONICAL isin, not the raw one. A
    face-value split issues a new ISIN, and the corp-action feed frequently
    reports a superseded identifier -- 22 of 60 price-adjusting events had an
    ISIN with no bars at all. Matching on the raw ISIN would leave those events
    permanently anchorless and the fake ex-date gap would survive into gold.

    ``DISTINCT ON`` with this ordering takes the most recent bar, preferring EQ
    over BE/BZ when a symbol trades in several series on the same day.
    """
    return """
        SELECT DISTINCT ON (e.event_id)
               e.event_id,
               b.trade_date AS anchor_date,
               b.c          AS anchor_price
          FROM silver.corp_action_event e
          JOIN silver.isin_link l
            ON l.canonical_isin = COALESCE(e.canonical_isin, e.isin)
          JOIN bronze.eod_bhav_raw b
            ON b.isin = l.isin
           AND b.trade_date < e.ex_date
         WHERE e.event_id = ANY(%s)
           AND b.c IS NOT NULL
           AND b.c > 0
         ORDER BY e.event_id,
                  b.trade_date DESC,
                  (b.series = 'EQ') DESC,
                  b.series
    """


def resolve_anchors(event_ids: list[int]) -> dict[int, tuple[dt.date, Decimal]]:
    """Look up (anchor_date, anchor_price) for each event id."""
    if not event_ids:
        return {}
    with connection() as conn, conn.cursor() as cur:
        cur.execute(_anchor_sql(), (event_ids,))
        return {
            r["event_id"]: (r["anchor_date"], r["anchor_price"]) for r in cur.fetchall()
        }


def _same_day_ordinary_dividend(conn, isin: str, ex_date: dt.date) -> Decimal:
    """Total ordinary (non-special) dividend on the same ex-date for this ISIN.

    Needed for Bloomberg's abnormal-cash treatment: when a special dividend
    shares its ex-date with an ordinary one, the ordinary part is removed from
    the base so only the abnormal component adjusts the price.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(div_amount), 0) AS total
              FROM silver.corp_action_event
             WHERE isin = %s
               AND ex_date = %s
               AND type IN ('DIVIDEND', 'INTERIM_DIVIDEND')
               AND superseded_at IS NULL
               AND div_amount IS NOT NULL
            """,
            (isin, ex_date),
        )
        row = cur.fetchone()
    return Decimal(str(row["total"])) if row and row["total"] is not None else Decimal(0)


def reset_factor_states(
    types: list[str] | None = None, only_missing_anchor: bool = False
) -> int:
    """Mark events for factor recomputation.

    Needed after any change to the factor or anchor logic: an event already in a
    terminal state (``computed`` / ``not_applicable``) is never revisited by
    ``compute_pending_factors``, so a logic fix would not reach existing rows.

    ``only_missing_anchor`` restricts the reset to events that lack an anchor
    price, which is the cheap targeted case (e.g. back-filling anchors onto
    dividends for the total-return series).
    """
    sql = """
        UPDATE silver.corp_action_event
           SET factor_state = 'pending', updated_at = now()
         WHERE superseded_at IS NULL
    """
    params: list = []
    if types:
        sql += " AND type = ANY(%s)"
        params.append(types)
    if only_missing_anchor:
        sql += " AND anchor_price IS NULL"
    n = execute(sql, params)
    log.info("factor_states_reset", rows=n, types=types, only_missing_anchor=only_missing_anchor)
    return n


def compute_pending_factors(
    limit: int | None = None, as_of: dt.date | None = None
) -> dict[str, int]:
    """Compute factors for every event still awaiting one.

    Returns counters. Safe to run repeatedly: an event already ``computed`` is
    not revisited, so this is idempotent.
    """
    stats = {
        "examined": 0,
        "computed": 0,
        "not_applicable": 0,
        "needs_anchor": 0,
        "unresolved": 0,
        "skipped_s_gt_p": 0,
        "errors": 0,
    }

    with connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT event_id, isin, symbol, ex_date, type, ratio_num, ratio_den,
                       sub_price, div_amount, face_value, is_special, parsed_ok,
                       raw_purpose
                  FROM silver.corp_action_event
                 WHERE superseded_at IS NULL
                   AND factor_state = ANY(%s)
            """
            params: list = [list(RETRY_STATES)]
            if as_of is not None:
                sql += " AND ex_date <= %s"
                params.append(as_of)
            sql += " ORDER BY ex_date"
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql, params)
            events = cur.fetchall()

        if not events:
            return stats

        stats["examined"] = len(events)

        # Anchors are needed by price-adjusting events AND by ordinary/interim
        # dividends.
        #
        # The dividend case is easy to miss: an ordinary dividend does not touch
        # the PRICE series, so it looks like it needs no anchor. But the parallel
        # TOTAL-RETURN series reinvests every dividend at ex-date, and that needs
        # P to form (P - D)/P. Resolving anchors only for PRICE_ADJUSTING types
        # left all 9,372 ordinary dividends with anchor_price = NULL, so
        # factor_tr silently stayed 1.0 and the *_tr columns were identical to
        # the price columns -- a total-return series with no total return in it.
        need_anchor_ids = [
            e["event_id"]
            for e in events
            if e["type"] in PRICE_ADJUSTING or e["type"] in DIVIDEND_TYPES
        ]
        anchors = resolve_anchors(need_anchor_ids) if need_anchor_ids else {}

        updates: list[tuple] = []
        for e in events:
            etype = e["type"]
            anchor_date, anchor_price = anchors.get(e["event_id"], (None, None))

            # An event the PARSER already rejected has no usable inputs, so
            # calling compute_factor on it only raises and logs a scary
            # "factor_error". Those 6 rows (partly-paid rights, rights with no
            # premium printed, rights with a warrant leg) are already in the
            # review queue awaiting a human. Routing them straight to
            # `unresolved` keeps the error counter meaningful: a FactorError
            # should mean a genuinely surprising input, not a known-unparseable
            # one, otherwise real problems hide among the expected noise.
            if not e["parsed_ok"]:
                stats["unresolved"] = stats.get("unresolved", 0) + 1
                updates.append(
                    (None, None, anchor_price, anchor_date, UNRESOLVED, e["event_id"])
                )
                continue

            try:
                if etype in ("SPECIAL_DIVIDEND", "RETURN_OF_CAPITAL") and anchor_price:
                    # Abnormal-cash treatment when an ordinary dividend shares
                    # the ex-date.
                    regular = _same_day_ordinary_dividend(conn, e["isin"], e["ex_date"])
                    if regular > 0:
                        from .factors import abnormal_cash_factor

                        f = abnormal_cash_factor(
                            anchor_price, regular, e["div_amount"] or 0
                        )
                    else:
                        f = compute_factor(
                            etype, div_amount=e["div_amount"], anchor_price=anchor_price
                        )
                else:
                    f = compute_factor(
                        etype,
                        ratio_num=e["ratio_num"],
                        ratio_den=e["ratio_den"],
                        sub_price=e["sub_price"],
                        div_amount=e["div_amount"],
                        anchor_price=anchor_price,
                    )
            except FactorError as exc:
                log.warning(
                    "factor_error",
                    event_id=e["event_id"],
                    isin=e["isin"],
                    type=etype,
                    purpose=e["raw_purpose"][:120],
                    error=str(exc),
                )
                stats["errors"] += 1
                updates.append(
                    (None, None, anchor_price, anchor_date, UNRESOLVED, e["event_id"])
                )
                continue

            state = f.state
            # A price-adjusting event with no anchor stays retryable.
            if etype in PRICE_ADJUSTING and anchor_price is None and etype not in (
                "BONUS",
                "SPLIT",
                "FACE_VALUE_CHANGE",
            ):
                state = NEEDS_ANCHOR

            stats[state] = stats.get(state, 0) + 1
            updates.append(
                (
                    f.factor_price,
                    f.factor_vol,
                    anchor_price,
                    anchor_date,
                    state,
                    e["event_id"],
                )
            )

        with conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE silver.corp_action_event
                   SET factor_price = %s,
                       factor_vol   = %s,
                       anchor_price = %s,
                       anchor_date  = %s,
                       factor_state = %s,
                       updated_at   = now()
                 WHERE event_id = %s
                """,
                updates,
            )

        # Apply manual overrides last: a human decision always wins.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE silver.corp_action_event e
                   SET factor_price = COALESCE(o.factor_price, e.factor_price),
                       factor_vol   = COALESCE(o.factor_vol,   e.factor_vol),
                       div_amount   = COALESCE(o.div_amount,   e.div_amount),
                       factor_state = 'computed',
                       updated_at   = now()
                  FROM silver.corp_action_override o
                 WHERE o.active
                   AND o.isin = e.isin
                   AND o.ex_date = e.ex_date
                   AND o.type = e.type
                   AND e.superseded_at IS NULL
                """
            )
            if cur.rowcount and cur.rowcount > 0:
                stats["overrides_applied"] = cur.rowcount
                log.info("overrides_applied", count=cur.rowcount)

    log.info("factors_computed", **stats)
    return stats


def affected_isins(since: dt.datetime | None = None) -> list[str]:
    """ISINs whose factors changed recently, i.e. whose gold rows must be rebuilt.

    A single new ex-date rescales a symbol's ENTIRE history, so the unit of
    rebuild is the ISIN, not the day.
    """
    sql = """
        SELECT DISTINCT isin
          FROM silver.corp_action_event
         WHERE superseded_at IS NULL
           AND factor_state = 'computed'
           AND factor_price IS NOT NULL
           AND factor_price <> 1
    """
    params: list = []
    if since is not None:
        sql += " AND updated_at >= %s"
        params.append(since)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [r["isin"] for r in cur.fetchall()]
