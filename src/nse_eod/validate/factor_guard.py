"""Zero-multiplier guard: catch factors that adjust nothing.

THE BUG CLASS. A ``silver.corp_action_event`` can carry a perfectly correct
factor filed against an ISIN that has no bars before its ex-date. The factor then
multiplies zero rows, the price is silently never adjusted, and **every existing
check passes**:

* ``right_edge_identity``  -- cum_factor is consistently 1.0, which is valid
* ``stale_factor``         -- a factor exists and is ``computed``
* ``prev_close_reconcile`` -- NSE's prev_close is unadjusted anyway

Observed: HDFC Bank's 2025 ``Bonus 1:1`` was filed against ``INE040A01018`` (the
pre-merger ISIN) while every bar sits under ``INE040A01034``. factor_price was
0.500000 and correct; it touched no row. India's largest private bank carried its
whole pre-August-2025 history unadjusted by 2x, and only a -50.4% entry in the
``unexplained_jumps`` list hinted at it.

**The detection must be CHAIN-AWARE.** Which ISIN you count bars for decides
whether this guard is usable at all. Measured over the live panel (1,426
non-identity factors):

    filed `isin`                   -> 271 flagged   (false positives: NSE files
                                                     superseded ISINs routinely
                                                     and isin_link resolves them)
    `canonical_isin` alone         -> 436 flagged   (WORSE -- the canonical ISIN is
                                                     the LATEST in a chain, so
                                                     TDPOWERSYS's INE419M01035 has
                                                     no bars before either of its
                                                     two split ex-dates)
    chain-aware via isin_link      ->   7 flagged   (matches reality)

Only the third mirrors ``adjusted.py::_fetch_bars``, which is the query that
actually does the multiplying. "Multiplies zero rows" is meaningless measured
against any other join.

**Two outcomes, not one.** Of the 7 chain-aware hits, all 7 have an ex-date
before the security's first available bar, so there is genuinely nothing to
adjust and no re-anchor could fix them:

    ORPHANED  -- the chain has NO bars before ex_date, but the SECURITY does
                 (under another symbol/identity). A wrong anchor. Actionable:
                 re-anchor. This is the HDFC Bank class. run-daily FAILS on it.
    PRE_HISTORY - no bars before ex_date anywhere. The event predates coverage.
                 Expected, unfixable, recorded at low severity. run-daily does
                 NOT fail on it, because a check that can never come back clean
                 gets muted, and then the real ones go unnoticed too.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from ..config import Settings, get_settings
from ..db import REVIEW_QUEUE_CONFLICT, connection, insert_ignore
from ..logging_setup import get_logger

log = get_logger(__name__)

ORPHANED = "orphaned_factor"
PRE_HISTORY = "pre_history"
PARSER_TAG = "factor_guard/1.0.0"

# Mirrors adjusted.py::_fetch_bars exactly: bars are located by joining
# bronze -> isin_link and grouping on the CANONICAL isin. Anything else measures
# a join that never runs.
_GUARD_SQL = """
WITH ev AS (
    SELECT e.event_id, e.symbol, e.isin, e.canonical_isin, e.ex_date, e.type,
           e.factor_price, e.factor_state, e.isin_resolved_via, e.raw_purpose,
           e.ca_raw_id,
           COALESCE(e.canonical_isin, e.isin) AS eff_isin
      FROM silver.corp_action_event e
     WHERE e.superseded_at IS NULL
       AND e.factor_price IS NOT NULL
       AND e.factor_price <> 1
       {extra_filter}
),
bars_in_chain AS (
    -- bars reachable for the event's effective ISIN, through the link chain
    SELECT ev.event_id, count(*) AS n
      FROM ev
      JOIN silver.isin_link l ON l.canonical_isin = ev.eff_isin
      JOIN bronze.eod_bhav_raw b
        ON b.isin = l.isin
       AND b.trade_date < ev.ex_date
     GROUP BY ev.event_id
),
bars_by_symbol AS (
    -- bars the SECURITY has before ex_date under ANY identity. This is what
    -- separates a wrong anchor (fixable) from a pre-coverage event (not).
    SELECT ev.event_id, count(*) AS n
      FROM ev
      JOIN bronze.eod_bhav_raw b
        ON b.symbol = ev.symbol
       AND b.trade_date < ev.ex_date
     GROUP BY ev.event_id
)
SELECT ev.*,
       COALESCE(bc.n, 0) AS bars_in_chain,
       COALESCE(bs.n, 0) AS bars_by_symbol
  FROM ev
  LEFT JOIN bars_in_chain bc ON bc.event_id = ev.event_id
  LEFT JOIN bars_by_symbol bs ON bs.event_id = ev.event_id
 WHERE COALESCE(bc.n, 0) = 0
 ORDER BY ev.ex_date DESC
"""


@dataclasses.dataclass
class OrphanFinding:
    """One factor that multiplies zero rows."""

    event_id: int
    symbol: str | None
    isin: str | None
    canonical_isin: str | None
    ex_date: dt.date
    type: str
    factor_price: float
    isin_resolved_via: str | None
    raw_purpose: str | None
    ca_raw_id: int | None
    bars_by_symbol: int

    @property
    def kind(self) -> str:
        """ORPHANED when the security has earlier bars elsewhere; else PRE_HISTORY."""
        return ORPHANED if self.bars_by_symbol > 0 else PRE_HISTORY

    @property
    def is_actionable(self) -> bool:
        return self.kind == ORPHANED

    def describe(self) -> str:
        if self.is_actionable:
            return (
                f"{self.symbol} {self.ex_date} {self.type} factor={self.factor_price:.6f} "
                f"filed on {self.isin} (resolved {self.canonical_isin or 'NONE'}) reaches 0 bars, "
                f"but the security has {self.bars_by_symbol} earlier bars under another "
                f"identity -- WRONG ANCHOR, re-anchor required"
            )
        return (
            f"{self.symbol} {self.ex_date} {self.type} factor={self.factor_price:.6f} "
            f"reaches 0 bars and the security has none before that date either "
            f"-- predates coverage, nothing to adjust"
        )


def assert_factors_multiply_rows(
    settings: Settings | None = None,
    since: dt.datetime | None = None,
    event_ids: list[int] | None = None,
) -> list[OrphanFinding]:
    """Return every non-identity factor that multiplies zero rows.

    ``since`` restricts to events touched at or after that timestamp, which is
    how ``run-daily`` asks "did tonight's ingest orphan anything?" without
    re-reporting the historical backlog every night.

    Raises nothing: the caller decides whether an orphan is fatal. Returning the
    findings rather than asserting keeps the same function usable by the one-time
    sweep, the nightly gate, and the tests.
    """
    s = settings or get_settings()
    extra = ""
    params: list = []
    if event_ids:
        extra += " AND e.event_id = ANY(%s)"
        params.append(event_ids)
    if since is not None:
        extra += " AND e.updated_at >= %s"
        params.append(since)

    sql = _GUARD_SQL.format(extra_filter=extra)
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    findings = [
        OrphanFinding(
            event_id=r["event_id"],
            symbol=r["symbol"],
            isin=r["isin"],
            canonical_isin=r["canonical_isin"],
            ex_date=r["ex_date"],
            type=r["type"],
            factor_price=float(r["factor_price"]),
            isin_resolved_via=r["isin_resolved_via"],
            raw_purpose=r["raw_purpose"],
            ca_raw_id=r["ca_raw_id"],
            bars_by_symbol=r["bars_by_symbol"],
        )
        for r in rows
    ]
    actionable = [f for f in findings if f.is_actionable]
    log.info(
        "factor_guard_checked",
        flagged=len(findings),
        orphaned=len(actionable),
        pre_history=len(findings) - len(actionable),
        scope="since=%s" % since if since else ("ids" if event_ids else "all"),
    )
    return findings


def queue_orphans(
    findings: list[OrphanFinding], settings: Settings | None = None
) -> dict[str, int]:
    """Write findings to the review queue. Idempotent on (ca_raw_id, raw_purpose).

    An ORPHANED factor goes in at severity=high because the symbol's adjusted
    history is wrong until someone re-anchors it. A PRE_HISTORY one goes in at
    severity=low: recorded for completeness, never on the critical path.
    """
    if not findings:
        return {"queued": 0, "orphaned": 0, "pre_history": 0}

    rows = []
    for f in findings:
        # A stable, self-describing purpose string: the unique index is on
        # (COALESCE(ca_raw_id,-1), raw_purpose), so this text IS the dedupe key.
        purpose = (
            f"[guard] {f.kind} event_id={f.event_id} {f.type} ex={f.ex_date} "
            f"factor={f.factor_price:.6f} filed_isin={f.isin} "
            f"resolved={f.canonical_isin or 'NONE'}"
        )
        rows.append(
            {
                "ca_raw_id": f.ca_raw_id,
                "isin": f.canonical_isin or f.isin,
                "symbol": f.symbol,
                "ex_date": f.ex_date,
                "raw_purpose": purpose,
                "reason": f.kind,
                "parser_version": PARSER_TAG,
                "suggested_type": f.type,
                "suggested_json": _suggestion_json(f),
                "severity": "high" if f.is_actionable else "low",
            }
        )

    with connection(settings) as conn:
        queued = insert_ignore(
            conn,
            "silver.corp_action_review_queue",
            rows,
            ["ca_raw_id", "raw_purpose"],
            conflict_target=REVIEW_QUEUE_CONFLICT,
        )

    stats = {
        "queued": queued,
        "orphaned": sum(1 for f in findings if f.is_actionable),
        "pre_history": sum(1 for f in findings if not f.is_actionable),
    }
    log.info("factor_guard_queued", **stats)
    return stats


def _suggestion_json(f: OrphanFinding) -> str:
    import json

    if f.is_actionable:
        return json.dumps(
            {
                "kind": ORPHANED,
                "event_id": f.event_id,
                "filed_isin": f.isin,
                "resolved_isin": f.canonical_isin,
                "factor_price": f"{f.factor_price:.12f}",
                "bars_before_ex_under_symbol": f.bars_by_symbol,
                "action": "re-anchor to the ISIN that holds the bars",
                "command": (
                    f"nse-eod review-queue resolve --review-id <ID> "
                    f"--action reanchor --to-isin <SURVIVING_ISIN> --confirm"
                ),
                "note": "NEVER auto-applied; requires explicit human confirmation",
            }
        )
    return json.dumps(
        {
            "kind": PRE_HISTORY,
            "event_id": f.event_id,
            "ex_date": str(f.ex_date),
            "action": "none available",
            "note": (
                "ex-date precedes all ingested bars for this security; no "
                "adjustment is possible or needed. Backfill earlier history if "
                "the pre-ex-date period is actually required."
            ),
        }
    )


def sweep(settings: Settings | None = None) -> dict[str, int]:
    """One-time sweep over ALL events; queues everything it finds."""
    findings = assert_factors_multiply_rows(settings)
    stats = queue_orphans(findings, settings)
    for f in findings:
        (log.warning if f.is_actionable else log.info)(
            "factor_guard_finding", kind=f.kind, detail=f.describe()
        )
    return stats


def gate(
    settings: Settings | None = None, since: dt.datetime | None = None
) -> tuple[list[OrphanFinding], int]:
    """The nightly gate. Returns (findings, exit_code).

    Exit code 2 when a NEW orphan appears -- an unadjusted security must never
    reach the trading layer silently. PRE_HISTORY findings never fail the run.
    """
    findings = assert_factors_multiply_rows(settings, since=since)
    queue_orphans(findings, settings)
    actionable = [f for f in findings if f.is_actionable]
    for f in actionable:
        log.error("factor_guard_orphan", detail=f.describe())
    return findings, (2 if actionable else 0)
