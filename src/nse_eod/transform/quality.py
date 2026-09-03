"""Per-ISIN verification status and the verified tradeable universe.

The gate the trading layer reads. An unverified security does not appear as a
candidate at all, rather than appearing with a warning flag nobody checks.

``status`` is DERIVED, never hand-set, from three inputs:

  1. open high-severity review items for that ISIN
  2. orphaned factors (see ``validate/factor_guard.py``)
  3. unresolved adjusted-prev_close breaks beyond the audit threshold

Thresholds are taken from ``tools/audit_panel.py`` verbatim rather than
re-chosen, so the gate and the audit can never disagree about what "a break" is:

    prev_close break : abs(prev_close - prior_close)/prior_close > 0.005
                       with prior bar within 7 days (adjacency guard)
    large move       : abs(c_adj/prior_c_adj - 1) > 0.25, c_adj > 5.0, in_universe

The one manual status is ``blocked``: set by a human to hold a name out
regardless of what the checks say. Recompute never overwrites it.
"""

from __future__ import annotations

import datetime as dt

from ..config import Settings, get_settings
from ..db import connection, upsert_rows
from ..logging_setup import get_logger

log = get_logger(__name__)

# --- thresholds lifted from tools/audit_panel.py; do not re-derive here -------
PREV_CLOSE_BREAK = 0.005      # audit_panel.py:330
ADJACENCY_DAYS = 7            # audit_panel.py:329
LARGE_MOVE = 0.25             # audit_panel.py:583
LARGE_MOVE_MIN_PRICE = 5.0    # audit_panel.py:575

VERIFIED = "verified"
REVIEW_OPEN = "review_open"
BLOCKED = "blocked"


_QUALITY_SQL = f"""
WITH universe_isins AS (
    -- every ISIN that reaches the panel; quality is only meaningful for these
    SELECT DISTINCT isin, (array_agg(symbol ORDER BY trade_date DESC))[1] AS symbol
      FROM gold.eod_adjusted
     GROUP BY isin
),
open_high AS (
    SELECT q.isin, count(*) AS n
      FROM silver.corp_action_review_queue q
     WHERE q.status = 'open' AND q.severity = 'high' AND q.isin IS NOT NULL
     GROUP BY q.isin
),
orphans AS (
    -- chain-aware, mirroring validate/factor_guard.py. A non-identity factor
    -- with zero reachable bars before its ex-date, where the security DOES have
    -- earlier bars under another identity (a wrong anchor, not pre-coverage).
    SELECT COALESCE(e.canonical_isin, e.isin) AS isin, count(*) AS n
      FROM silver.corp_action_event e
     WHERE e.superseded_at IS NULL
       AND e.factor_price IS NOT NULL AND e.factor_price <> 1
       AND NOT EXISTS (
             SELECT 1 FROM silver.isin_link l
               JOIN bronze.eod_bhav_raw b ON b.isin = l.isin
              WHERE l.canonical_isin = COALESCE(e.canonical_isin, e.isin)
                AND b.trade_date < e.ex_date
           )
       AND EXISTS (
             SELECT 1 FROM bronze.eod_bhav_raw b2
              WHERE b2.symbol = e.symbol AND b2.trade_date < e.ex_date
           )
     GROUP BY 1
),
breaks AS (
    -- Unresolved prev_close breaks on the ADJUSTED panel, using the audit's own
    -- threshold and adjacency guard. Chained per ISIN across series: a security
    -- migrates between series and `series` is an attribute of a bar, not an
    -- identity, so chaining per (isin, series) invents phantom breaks.
    SELECT isin, count(*) AS n FROM (
        SELECT g.isin, g.trade_date, g.c_raw,
               lag(g.c_raw)      OVER w AS prior_c,
               lag(g.trade_date) OVER w AS prior_d,
               b.prev_close
          FROM (
                SELECT DISTINCT ON (isin, trade_date) isin, trade_date, c_raw, isin_traded, series
                  FROM gold.eod_adjusted
                 ORDER BY isin, trade_date, (series = 'EQ') DESC, series
               ) g
          JOIN bronze.eod_bhav_raw b
            ON b.isin = g.isin_traded AND b.trade_date = g.trade_date
                                      AND b.series = g.series
        WINDOW w AS (PARTITION BY g.isin ORDER BY g.trade_date)
    ) t
     WHERE prior_c IS NOT NULL AND prior_c > 0
       AND prev_close IS NOT NULL AND prev_close > 0
       AND prior_d >= trade_date - INTERVAL '{ADJACENCY_DAYS} days'
       AND abs(prev_close - prior_c) / prior_c > {PREV_CLOSE_BREAK}
     GROUP BY isin
)
SELECT u.isin,
       u.symbol,
       COALESCE(oh.n, 0) AS open_high_sev,
       COALESCE(o.n, 0)  AS orphaned_factors,
       COALESCE(br.n, 0) AS unresolved_breaks
  FROM universe_isins u
  LEFT JOIN open_high oh ON oh.isin = u.isin
  LEFT JOIN orphans   o  ON o.isin  = u.isin
  LEFT JOIN breaks    br ON br.isin = u.isin
"""


def recompute_security_quality(settings: Settings | None = None) -> dict[str, int]:
    """Rebuild ``silver.security_quality`` from current evidence.

    Idempotent. ``blocked`` rows are preserved: that status is a human decision
    and no automatic recompute may lift it.
    """
    s = settings or get_settings()
    with connection(s) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT isin FROM silver.security_quality WHERE status = 'blocked'")
            blocked = {r["isin"] for r in cur.fetchall()}

        with conn.cursor() as cur:
            cur.execute(_QUALITY_SQL)
            rows = cur.fetchall()

        now = dt.datetime.now(dt.timezone.utc)
        payload = []
        counts = {VERIFIED: 0, REVIEW_OPEN: 0, BLOCKED: 0}
        for r in rows:
            problems = (
                int(r["open_high_sev"]) + int(r["orphaned_factors"]) + int(r["unresolved_breaks"])
            )
            if r["isin"] in blocked:
                status = BLOCKED
            elif problems == 0:
                status = VERIFIED
            else:
                status = REVIEW_OPEN
            counts[status] += 1

            reasons = []
            if r["open_high_sev"]:
                reasons.append(f"{r['open_high_sev']} open high-severity review item(s)")
            if r["orphaned_factors"]:
                reasons.append(f"{r['orphaned_factors']} orphaned factor(s)")
            if r["unresolved_breaks"]:
                reasons.append(
                    f"{r['unresolved_breaks']} prev_close break(s) > "
                    f"{PREV_CLOSE_BREAK*100:g}%"
                )
            payload.append(
                {
                    "isin": r["isin"],
                    "symbol": r["symbol"],
                    "status": status,
                    "open_high_sev": int(r["open_high_sev"]),
                    "orphaned_factors": int(r["orphaned_factors"]),
                    "unresolved_breaks": int(r["unresolved_breaks"]),
                    "note": "; ".join(reasons) or None,
                    "updated_at": now,
                }
            )

        upsert_rows(
            conn,
            "silver.security_quality",
            payload,
            conflict_cols=["isin"],
            update_cols=[
                "symbol", "status", "open_high_sev", "orphaned_factors",
                "unresolved_breaks", "note", "updated_at",
            ],
        )

    stats = {
        "examined": len(rows),
        "verified": counts[VERIFIED],
        "review_open": counts[REVIEW_OPEN],
        "blocked": counts[BLOCKED],
    }
    log.info("security_quality_recomputed", **stats)
    return stats


def rebuild_tradeable_universe(
    settings: Settings | None = None, isins: list[str] | None = None
) -> dict[str, int]:
    """Materialize ``gold.tradeable_universe`` = in_universe AND status='verified'.

    Call ONLY after the panel is built and every validation check has passed. The
    table is deliberately not a view: on a failed run the previous, known-good
    universe stays in place rather than the gate silently widening to include
    prices nothing has verified.

    ``isins`` scopes the refresh to specific securities, for the per-ISIN
    resolution path. A scoped refresh must DELETE those ISINs' rows first so a
    name that has just become unverified actually leaves the universe.
    """
    s = settings or get_settings()
    with connection(s) as conn:
        with conn.cursor() as cur:
            if isins:
                cur.execute(
                    "DELETE FROM gold.tradeable_universe WHERE isin = ANY(%s)", (isins,)
                )
                removed = max(cur.rowcount, 0)
            else:
                # TRUNCATE reports rowcount -1, and `-1 or 0` is truthy so the -1
                # flowed straight into the ops log as rows_removed=-1. Counting first
                # is one seq scan on a table we are about to empty anyway, and it makes
                # removed-vs-written a real churn signal instead of a constant.
                cur.execute("SELECT count(*) AS n FROM gold.tradeable_universe")
                removed = cur.fetchone()["n"]
                cur.execute("TRUNCATE gold.tradeable_universe")

        scope = "AND g.isin = ANY(%s)" if isins else ""
        params = [isins] if isins else []
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO gold.tradeable_universe
                    (trade_date, isin, symbol, series_flag, adv_20, c_adj,
                     quality_status, built_at)
                SELECT g.trade_date, g.isin, g.symbol, g.series_flag, g.adv_20,
                       g.c_adj, q.status, now()
                  FROM gold.eod_adjusted g
                  JOIN silver.security_quality q ON q.isin = g.isin
                 WHERE g.in_universe
                   AND q.status = 'verified'
                   {scope}
                ON CONFLICT (trade_date, isin) DO UPDATE
                   SET symbol = EXCLUDED.symbol,
                       series_flag = EXCLUDED.series_flag,
                       adv_20 = EXCLUDED.adv_20,
                       c_adj = EXCLUDED.c_adj,
                       quality_status = EXCLUDED.quality_status,
                       built_at = EXCLUDED.built_at
                """,
                params,
            )
            inserted = max(cur.rowcount, 0)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS rows, count(DISTINCT isin) AS securities,
                       max(trade_date) AS latest
                  FROM gold.tradeable_universe
                """
            )
            tot = cur.fetchone()

    stats = {
        "rows_removed": removed,
        "rows_written": inserted,
        "universe_rows": tot["rows"],
        "universe_securities": tot["securities"],
    }
    log.info("tradeable_universe_rebuilt", latest=str(tot["latest"]), **stats)
    return stats


def quarantine_isins(
    isins: list[str], note: str, settings: Settings | None = None
) -> dict[str, int]:
    """Immediately eject specific ISINs from the universe. FAIL CLOSED.

    Needed because freezing the universe on a failed run is only PARTIAL
    containment. Observed while testing the gate: breaking HDFC Bank's anchor
    made run-daily exit 2, so the universe rebuild was skipped and the previous
    membership stayed frozen -- correct so far. But ``gold.eod_adjusted`` had
    ALREADY been rewritten with the unadjusted prices, and
    ``security_quality`` had NOT been recomputed, so HDFC Bank sat in
    ``tradeable_universe`` still marked ``verified`` while its underlying prices
    were wrong by 2x. A consumer joining the universe back to gold for a full
    price series would have read the broken data.

    So a detected problem must EJECT the affected names rather than merely
    decline to re-admit them. Called from the guard path in run-daily, before
    the run aborts.
    """
    if not isins:
        return {"quarantined": 0, "universe_rows_removed": 0}

    now = dt.datetime.now(dt.timezone.utc)
    with connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO silver.security_quality
                    (isin, status, note, updated_at)
                SELECT x.isin, 'review_open', %s, %s
                  FROM unnest(%s::text[]) AS x(isin)
                ON CONFLICT (isin) DO UPDATE
                   SET status = 'review_open',
                       note = EXCLUDED.note,
                       updated_at = EXCLUDED.updated_at
                 WHERE silver.security_quality.status <> 'blocked'
                """,
                (note, now, isins),
            )
            flagged = max(cur.rowcount, 0)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM gold.tradeable_universe WHERE isin = ANY(%s)", (isins,)
            )
            removed = max(cur.rowcount, 0)

    stats = {"quarantined": flagged, "universe_rows_removed": removed}
    log.warning("isins_quarantined", isins=isins, note=note, **stats)
    return stats


def refresh(settings: Settings | None = None, isins: list[str] | None = None) -> dict:
    """Recompute quality, then rebuild the universe. The order matters."""
    q = recompute_security_quality(settings)
    u = rebuild_tradeable_universe(settings, isins=isins)
    return {"quality": q, "universe": u}


def isin_status(isin: str, settings: Settings | None = None) -> dict | None:
    from ..db import fetch_one

    return fetch_one(
        """
        SELECT q.*,
               (SELECT count(*) FROM gold.tradeable_universe u WHERE u.isin = q.isin)
                   AS universe_rows,
               (SELECT max(trade_date) FROM gold.eod_adjusted g WHERE g.isin = q.isin)
                   AS last_bar
          FROM silver.security_quality q
         WHERE q.isin = %s
        """,
        (isin,),
    )
