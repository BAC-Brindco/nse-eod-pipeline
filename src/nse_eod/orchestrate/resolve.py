"""Human-in-the-loop resolution of review-queue items.

Nothing here derives, infers, or guesses a corporate-action number. Every path
requires a value a human supplies and, for demergers, the provenance of that
value. The pipeline's job is to make RECORDING a resolution safe and to VERIFY
that the recorded resolution actually clears the flagged break -- not to invent
the number.

Three categories, three different human inputs, deliberately kept separate:

  needs_external_price   demerger / spin-off. Needs the spun-off entity's value
                         from the NSE demerger circular or the special pre-open
                         price. ``--source`` is MANDATORY: the number cannot be
                         derived from NSE EOD data, so its provenance IS the
                         audit trail.
  inferred_isin_switch   an ISIN changed with no corporate action to explain it.
                         Needs a ratio, or a re-anchor if the factor is filed on
                         the wrong identity.
  orphaned_factor        the factor is correct but filed against an ISIN with no
                         bars. Needs a re-anchor to the surviving ISIN. See
                         validate/factor_guard.py.
  large_move_unexplained a >25% adjusted move with no action on file. May be a
                         GENUINE move (YESBANK's 2020 moratorium, INDUSINDBK's
                         COVID rebound), in which case the resolution is
                         ``--action accept``, closing it as 'reviewed_accepted'
                         without writing any factor.

Safety properties:
  * Never rebuilds the full panel. Corrections are scoped to the affected ISINs
    via ``materialize(isins=[...])``.
  * Never marks an item resolved unless the flagged break actually clears. If the
    supplied number does not fix it, the write is KEPT (a human decision belongs
    on the record) but the item stays open and the ISIN stays unverified.
  * Bronze is never touched.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal, InvalidOperation

from ..config import Settings, get_settings
from ..db import connection, execute, fetch_all, fetch_one
from ..logging_setup import get_logger
from ..transform import quality
from ..transform.factors import COMPUTED
from ..validate import factor_guard

log = get_logger(__name__)

ACTION_FACTOR = "factor"       # human supplies factor_price directly
ACTION_REANCHOR = "reanchor"   # re-point the event at the ISIN holding the bars
ACTION_ACCEPT = "accept"       # confirmed genuine move; no factor written
ACTION_IGNORE = "ignore"       # not applicable; close without action

ACTIONS = (ACTION_FACTOR, ACTION_REANCHOR, ACTION_ACCEPT, ACTION_IGNORE)

# Reasons that MUST carry a --source: the number is unobtainable from NSE EOD
# data, so provenance is the only audit trail there can be.
SOURCE_REQUIRED_REASONS = {"needs_external_price"}


class ResolveError(RuntimeError):
    """The resolution cannot be recorded safely."""


@dataclasses.dataclass
class ResolveOutcome:
    review_id: int
    action: str
    isins: list[str]
    break_cleared: bool | None
    status_before: str | None
    status_after: str | None
    marked: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.marked in ("resolved", "reviewed_accepted", "ignored")


# --------------------------------------------------------------------- listing
def list_items(
    severity: str | None = "high",
    in_universe_only: bool = False,
    reason: str | None = None,
    limit: int = 40,
    settings: Settings | None = None,
) -> list[dict]:
    """Open items, UNIVERSE MEMBERS FIRST.

    The ordering is the point. 105 of the open high-severity items are illiquid
    SME ISIN-switches that will never be traded; sorted alongside a mega-cap they
    sit on the critical path forever. Universe membership then ADV puts the names
    that actually gate a trade at the top.
    """
    sql = """
        SELECT q.review_id, q.symbol, q.isin, q.ex_date, q.reason, q.severity,
               q.raw_purpose, q.suggested_type, q.suggested_json, q.created_at,
               sq.status AS quality_status,
               EXISTS (
                   SELECT 1 FROM gold.eod_adjusted g
                    WHERE g.isin = q.isin AND g.in_universe
               ) AS in_universe,
               (SELECT max(g.adv_20) FROM gold.eod_adjusted g WHERE g.isin = q.isin)
                   AS adv_20
          FROM silver.corp_action_review_queue q
          LEFT JOIN silver.security_quality sq ON sq.isin = q.isin
         WHERE q.status = 'open'
    """
    params: list = []
    if severity:
        sql += " AND q.severity = %s"
        params.append(severity)
    if reason:
        sql += " AND q.reason = %s"
        params.append(reason)
    if in_universe_only:
        sql += """
           AND EXISTS (SELECT 1 FROM gold.eod_adjusted g
                        WHERE g.isin = q.isin AND g.in_universe)
        """
    sql += """
        ORDER BY in_universe DESC,
                 adv_20 DESC NULLS LAST,
                 q.ex_date DESC NULLS LAST
        LIMIT %s
    """
    params.append(limit)
    return fetch_all(sql, params)


# ------------------------------------------------------------------ resolution
def _dec(v) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ResolveError(f"not a number: {v!r}") from exc
    if not (Decimal("0.000001") <= d <= Decimal("1000")):
        raise ResolveError(
            f"factor {d} outside the plausible range (0.000001 .. 1000); a factor is "
            "a fraction for a dilutive event or >1 for a consolidation"
        )
    return d


def _item(review_id: int) -> dict:
    row = fetch_one(
        "SELECT * FROM silver.corp_action_review_queue WHERE review_id = %s", (review_id,)
    )
    if row is None:
        raise ResolveError(f"review_id {review_id} not found")
    if row["status"] != "open":
        raise ResolveError(f"review_id {review_id} is already {row['status']}")
    return row


def _event_for(item: dict, event_id: int | None) -> dict | None:
    """Locate the corp_action_event this item refers to."""
    if event_id is not None:
        ev = fetch_one(
            "SELECT * FROM silver.corp_action_event WHERE event_id = %s", (event_id,)
        )
        if ev is None:
            raise ResolveError(f"event_id {event_id} not found")
        return ev
    # guard-generated purposes embed the id: "[guard] <kind> event_id=NNN ..."
    purpose = item["raw_purpose"] or ""
    if "event_id=" in purpose:
        try:
            token = purpose.split("event_id=", 1)[1].split()[0]
            return fetch_one(
                "SELECT * FROM silver.corp_action_event WHERE event_id = %s", (int(token),)
            )
        except (ValueError, IndexError):
            pass
    if item["ca_raw_id"] is not None:
        rows = fetch_all(
            """
            SELECT * FROM silver.corp_action_event
             WHERE ca_raw_id = %s AND superseded_at IS NULL
             ORDER BY component_ix
            """,
            (item["ca_raw_id"],),
        )
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise ResolveError(
                f"{len(rows)} events share ca_raw_id {item['ca_raw_id']}; "
                "pass --event-id to disambiguate"
            )
    return None


def _count_breaks(isins: list[str], settings: Settings | None = None) -> int:
    """prev_close breaks for these ISINs, using the AUDIT's own threshold."""
    if not isins:
        return 0
    r = fetch_one(
        f"""
        SELECT count(*) AS n FROM (
            SELECT g.isin, g.trade_date, g.c_raw,
                   lag(g.c_raw)      OVER w AS prior_c,
                   lag(g.trade_date) OVER w AS prior_d,
                   b.prev_close
              FROM (
                    SELECT DISTINCT ON (isin, trade_date)
                           isin, trade_date, c_raw, isin_traded, series
                      FROM gold.eod_adjusted
                     WHERE isin = ANY(%s)
                     ORDER BY isin, trade_date, (series = 'EQ') DESC, series
                   ) g
              JOIN bronze.eod_bhav_raw b
                ON b.isin = g.isin_traded AND b.trade_date = g.trade_date
                                          AND b.series = g.series
            WINDOW w AS (PARTITION BY g.isin ORDER BY g.trade_date)
        ) t
         WHERE prior_c IS NOT NULL AND prior_c > 0
           AND prev_close IS NOT NULL AND prev_close > 0
           AND prior_d >= trade_date - INTERVAL '{quality.ADJACENCY_DAYS} days'
           AND abs(prev_close - prior_c) / prior_c > {quality.PREV_CLOSE_BREAK}
        """,
        (isins,),
    )
    return int(r["n"]) if r else 0


def _count_large_moves(isins: list[str]) -> int:
    """Unexplained >25% adjusted moves, using the AUDIT's threshold and floor."""
    if not isins:
        return 0
    r = fetch_one(
        f"""
        SELECT count(*) AS n FROM (
            SELECT g.isin, g.trade_date, g.c_adj,
                   lag(g.c_adj)      OVER w AS prior_c,
                   lag(g.trade_date) OVER w AS prior_d
              FROM gold.eod_adjusted g
             WHERE g.isin = ANY(%s) AND g.in_universe
               AND g.c_adj > {quality.LARGE_MOVE_MIN_PRICE}
            WINDOW w AS (PARTITION BY g.isin, g.series ORDER BY g.trade_date)
        ) c
         WHERE c.prior_c IS NOT NULL AND c.prior_c > 0
           AND c.prior_d >= c.trade_date - INTERVAL '{quality.ADJACENCY_DAYS} days'
           AND abs(c.c_adj / c.prior_c - 1) > {quality.LARGE_MOVE}
           AND NOT EXISTS (
                 SELECT 1 FROM silver.corp_action_event e
                  WHERE COALESCE(e.canonical_isin, e.isin) = c.isin
                    AND e.ex_date BETWEEN c.prior_d - 3 AND c.trade_date + 3
                    AND e.superseded_at IS NULL
               )
        """,
        (isins,),
    )
    return int(r["n"]) if r else 0


def resolve(
    review_id: int,
    action: str,
    factor: str | None = None,
    to_isin: str | None = None,
    source: str | None = None,
    note: str | None = None,
    event_id: int | None = None,
    resolved_by: str = "manual",
    confirm: bool = False,
    settings: Settings | None = None,
) -> ResolveOutcome:
    """Record a human-supplied resolution, rebuild scoped, verify, then mark.

    ``confirm`` is mandatory for anything that writes a factor or re-anchors an
    event: those change prices, and no corporate-action number is ever applied
    without explicit human confirmation.
    """
    s = settings or get_settings()
    if action not in ACTIONS:
        raise ResolveError(f"action must be one of {ACTIONS}, got {action!r}")

    item = _item(review_id)
    reason = item["reason"]

    if action in (ACTION_FACTOR, ACTION_REANCHOR) and not confirm:
        raise ResolveError(
            f"action '{action}' changes adjusted prices and requires --confirm. "
            "Nothing is applied without explicit human confirmation."
        )
    if reason in SOURCE_REQUIRED_REASONS and action == ACTION_FACTOR and not source:
        raise ResolveError(
            f"reason '{reason}' needs --source: the number cannot be derived from NSE "
            "EOD data, so its provenance (e.g. 'NSE demerger circular 2026/041' or "
            "'special pre-open price 2026-07-22') is the audit trail"
        )

    ev = _event_for(item, event_id)
    target_isin = item["isin"]
    if ev is not None:
        target_isin = ev.get("canonical_isin") or ev.get("isin") or target_isin

    # ---- close-only paths: no price change, no rebuild --------------------
    if action in (ACTION_ACCEPT, ACTION_IGNORE):
        marked = "reviewed_accepted" if action == ACTION_ACCEPT else "ignored"
        execute(
            """
            UPDATE silver.corp_action_review_queue
               SET status = %s, resolution_note = %s, resolution_source = %s,
                   resolved_by = %s, resolved_at = now()
             WHERE review_id = %s
            """,
            (
                marked,
                note
                or (
                    "confirmed a genuine market move, not a corporate action"
                    if action == ACTION_ACCEPT
                    else "not applicable"
                ),
                source,
                resolved_by,
                review_id,
            ),
        )
        status_before = _quality_status(target_isin)
        # One fewer open high-severity item can flip this ISIN to verified.
        quality.refresh(s, isins=[target_isin] if target_isin else None)
        status_after = _quality_status(target_isin)
        log.info(
            "review_closed",
            review_id=review_id,
            action=action,
            marked=marked,
            isin=target_isin,
            status_after=status_after,
        )
        return ResolveOutcome(
            review_id=review_id,
            action=action,
            isins=[target_isin] if target_isin else [],
            break_cleared=None,
            status_before=status_before,
            status_after=status_after,
            marked=marked,
            detail=f"closed as {marked}; no factor written",
        )

    # ---- price-changing paths -------------------------------------------
    if ev is None:
        raise ResolveError(
            f"review_id {review_id} has no linked corp_action_event; pass --event-id"
        )

    ex_date = ev["ex_date"]
    if ex_date is None:
        raise ResolveError("event has no ex_date; the ex-date is the adjustment anchor")

    if action == ACTION_REANCHOR:
        if not to_isin:
            raise ResolveError("--to-isin is required for action 'reanchor'")
        target_isin = to_isin.strip().upper()
        _assert_reanchor_target_holds_bars(target_isin, ex_date)
        new_factor = None
    else:
        if factor is None:
            raise ResolveError("--factor is required for action 'factor'")
        new_factor = _dec(factor)

    isins_before = sorted(
        {i for i in (ev.get("canonical_isin"), ev.get("isin"), target_isin) if i}
    )
    breaks_before = _count_breaks(isins_before, s)
    moves_before = _count_large_moves(isins_before)
    status_before = _quality_status(target_isin)

    # ---- write the resolution -------------------------------------------
    with connection(s) as conn, conn.cursor() as cur:
        if action == ACTION_REANCHOR:
            cur.execute(
                """
                UPDATE silver.corp_action_event
                   SET canonical_isin = %s,
                       isin_resolved_via = 'manual_reanchor',
                       factor_state = 'pending',
                       updated_at = now()
                 WHERE event_id = %s
                """,
                (target_isin, ev["event_id"]),
            )
        else:
            # Recorded as an OVERRIDE keyed on (isin, ex_date, type), so the
            # parser can be re-run freely without losing the human decision.
            cur.execute(
                """
                INSERT INTO silver.corp_action_override
                    (isin, ex_date, type, factor_price, note, created_by, active)
                VALUES (%s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (isin, ex_date, type) DO UPDATE
                   SET factor_price = EXCLUDED.factor_price,
                       note = EXCLUDED.note,
                       created_by = EXCLUDED.created_by,
                       active = true
                """,
                (
                    target_isin,
                    ex_date,
                    ev["type"],
                    new_factor,
                    json.dumps(
                        {
                            "review_id": review_id,
                            "event_id": ev["event_id"],
                            "source": source,
                            "note": note,
                            "reason": reason,
                        }
                    ),
                    resolved_by,
                ),
            )
            cur.execute(
                """
                UPDATE silver.corp_action_event
                   SET factor_price = %s, factor_state = %s, updated_at = now()
                 WHERE event_id = %s
                """,
                (new_factor, COMPUTED, ev["event_id"]),
            )

    # ---- recompute factors, then SCOPED rebuild -------------------------
    from ..transform.adjusted import materialize
    from ..transform.anchors import compute_pending_factors

    if action == ACTION_REANCHOR:
        # Only this event is pending, so this is cheap and touches nothing else.
        compute_pending_factors()

    scope = sorted(set(isins_before) | {target_isin})
    mstats = materialize(scope, settings=s)
    log.info("resolve_scoped_rebuild", isins=scope, rows=mstats["rows"])

    # ---- verify the break actually cleared ------------------------------
    breaks_after = _count_breaks(scope, s)
    moves_after = _count_large_moves(scope)
    orphans_after = [
        f
        for f in factor_guard.assert_factors_multiply_rows(s, event_ids=[ev["event_id"]])
        if f.is_actionable
    ]
    cleared = (
        breaks_after == 0 and moves_after == 0 and not orphans_after
    ) or (
        breaks_after <= breaks_before
        and moves_after < moves_before
        and not orphans_after
    )

    quality.refresh(s, isins=scope)
    status_after = _quality_status(target_isin)

    detail = (
        f"breaks {breaks_before}->{breaks_after}, unexplained moves "
        f"{moves_before}->{moves_after}, orphans_after={len(orphans_after)}"
    )

    if cleared:
        execute(
            """
            UPDATE silver.corp_action_review_queue
               SET status = 'resolved', resolution_note = %s, resolution_source = %s,
                   resolved_event_id = %s, resolved_by = %s, resolved_at = now()
             WHERE review_id = %s
            """,
            (
                f"{action}: {note or ''} [{detail}]".strip(),
                source,
                ev["event_id"],
                resolved_by,
                review_id,
            ),
        )
        marked = "resolved"
        # Re-derive quality now the item is closed, so status can reach verified.
        quality.refresh(s, isins=scope)
        status_after = _quality_status(target_isin)
    else:
        # The human decision is KEPT on the record, but the item stays open and
        # the ISIN stays unverified: the supplied number did not clear the flag,
        # so the price is still not trustworthy.
        execute(
            """
            UPDATE silver.corp_action_review_queue
               SET resolution_note = %s, resolution_source = %s
             WHERE review_id = %s
            """,
            (
                f"ATTEMPTED {action} did NOT clear the flag; item left open. "
                f"{note or ''} [{detail}]".strip(),
                source,
                review_id,
            ),
        )
        marked = "open"
        log.warning(
            "resolve_did_not_clear", review_id=review_id, action=action, detail=detail
        )

    return ResolveOutcome(
        review_id=review_id,
        action=action,
        isins=scope,
        break_cleared=cleared,
        status_before=status_before,
        status_after=status_after,
        marked=marked,
        detail=detail,
    )


def _assert_reanchor_target_holds_bars(isin: str, ex_date: dt.date) -> None:
    """A re-anchor target must actually hold bars before the ex-date.

    Otherwise the re-anchor just MOVES the orphan, which is the exact failure
    this workflow exists to prevent.
    """
    r = fetch_one(
        """
        SELECT count(*) AS n
          FROM silver.isin_link l
          JOIN bronze.eod_bhav_raw b ON b.isin = l.isin
         WHERE l.canonical_isin = %s AND b.trade_date < %s
        """,
        (isin, ex_date),
    )
    n = int(r["n"]) if r else 0
    if n == 0:
        raise ResolveError(
            f"re-anchor target {isin} has NO bars before ex-date {ex_date} — this "
            "would just move the orphan. Pass the surviving/post-switch ISIN that "
            "actually holds the pre-ex-date history."
        )


def _quality_status(isin: str | None) -> str | None:
    if not isin:
        return None
    r = fetch_one("SELECT status FROM silver.security_quality WHERE isin = %s", (isin,))
    return r["status"] if r else None


# ------------------------------------------------- relabel the switch backlog
def relabel_inferred_isin_switches(settings: Settings | None = None) -> int:
    """Give the derived ISIN-switch rows their own reason.

    They arrive as reason='unparsed_pattern' with a '[derived] ISIN changed ...'
    purpose, which lumps them in with genuine parse failures. The three A3
    categories must be separable because each takes a DIFFERENT human input: a
    demerger needs a spun-off value, a switch needs a ratio or re-anchor, a large
    move needs confirmation. Preserves created_at and existing notes.
    """
    n = execute(
        """
        UPDATE silver.corp_action_review_queue
           SET reason = 'inferred_isin_switch'
         WHERE status = 'open'
           AND reason = 'unparsed_pattern'
           AND raw_purpose LIKE '[derived] ISIN changed%'
        """
    )
    log.info("relabelled_inferred_isin_switches", rows=n)
    return n
