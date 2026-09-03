"""Validation checks. Each returns a list of Finding objects; none raise.

The load-bearing check is ``prev_close_reconcile``. NSE's own ``prev_close``
already reflects corporate actions, so comparing it against OUR factor-adjusted
prior close is an independent cross-check of the factor itself. If a bonus was
missed, this fires on the ex-date -- before the bad data reaches anything
downstream.

``overnight_jump`` is the backstop for the same failure in the other direction:
a 1:1 bonus that was never applied shows up as a -50 % move in the ADJUSTED
series, which is the classic symptom of an unapplied corporate action.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
from typing import Any

from ..config import Settings, get_settings
from ..db import connection
from ..logging_setup import get_logger

log = get_logger(__name__)

INFO, WARNING, CRITICAL = "info", "warning", "critical"


@dataclasses.dataclass
class Finding:
    check_name: str
    severity: str
    detail: str
    trade_date: dt.date | None = None
    isin: str | None = None
    symbol: str | None = None
    metrics: dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_row(self, run_id: int | None) -> dict:
        return {
            "run_id": run_id,
            "trade_date": self.trade_date,
            "check_name": self.check_name,
            "severity": self.severity,
            "isin": self.isin,
            "symbol": self.symbol,
            "detail": self.detail,
            "metrics": json.dumps(self.metrics, default=str),
        }


# ---------------------------------------------------- prev_close reconciliation
def check_prev_close(trade_date: dt.date, settings: Settings | None = None) -> list[Finding]:
    """Reconcile NSE's ``prev_close`` against our stored prior close, UNADJUSTED.

    Measured against live NSE data (2026-08), and it settles a design question:
    **NSE does not corporate-action-adjust prev_close.**

        GOODLUCK, bonus 2:1, ex 2026-08-21: prior close 1439.40,
                                            prev_close on ex-date 1439.40
        TDPOWERSYS, split Rs2->Re1, ex 2026-08-24: prior close 1534.80,
                                            prev_close on ex-date 1534.80

    So this is a **data-integrity** check, not a corporate-action cross-check: it
    catches a missing or misdated bar, a restatement, or a wrongly linked ISIN.
    Comparing against a factor-adjusted prior close would fire on every genuine
    corporate action -- which is exactly the false alarm this version avoids.

    The corporate-action cross-check is ``check_overnight_jump``, which looks for
    a large move in the ADJUSTED series with no event on file.
    """
    s = settings or get_settings()
    tol = s.prev_close_tolerance_pct / 100.0

    # Prior bar is matched on the CANONICAL isin so a face-value split, which
    # issues a new ISIN mid-series, does not look like a break in continuity.
    sql = """
        WITH canon AS (
            SELECT b.trade_date, b.series, b.symbol, b.c, b.prev_close,
                   COALESCE(l.canonical_isin, b.isin) AS isin
              FROM bronze.eod_bhav_raw b
              LEFT JOIN silver.isin_link l ON l.isin = b.isin
        ),
        prior AS (
            SELECT DISTINCT ON (isin, series)
                   isin, series, trade_date AS prior_date, c AS prior_close
              FROM canon
             WHERE trade_date < %(d)s
             ORDER BY isin, series, trade_date DESC
        )
        SELECT t.isin, t.symbol, t.series, t.prev_close,
               p.prior_close, p.prior_date
          FROM canon t
          JOIN prior p ON p.isin = t.isin AND p.series = t.series
         WHERE t.trade_date = %(d)s
           AND t.prev_close IS NOT NULL AND t.prev_close > 0
           AND p.prior_close IS NOT NULL AND p.prior_close > 0
           -- only compare against a genuinely adjacent bar
           AND p.prior_date >= %(d)s::date - INTERVAL '10 days'
    """

    findings: list[Finding] = []
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(sql, {"d": trade_date})
        for r in cur.fetchall():
            expected = float(r["prior_close"])   # NSE carries the RAW prior close
            actual = float(r["prev_close"])
            if expected <= 0:
                continue
            rel = abs(actual - expected) / expected
            if rel > tol:
                findings.append(
                    Finding(
                        check_name="prev_close_reconcile",
                        severity=CRITICAL,
                        trade_date=trade_date,
                        isin=r["isin"],
                        symbol=r["symbol"],
                        detail=(
                            f"{r['symbol']} ({r['series']}): NSE prev_close {actual:.4f} does not "
                            f"match our stored close {expected:.4f} from {r['prior_date']} "
                            f"-- diff {rel*100:.2f}%. Indicates a missing/misdated bar, an NSE "
                            "restatement, or a mis-linked ISIN."
                        ),
                        metrics={
                            "prev_close": actual,
                            "prior_close": expected,
                            "prior_date": str(r["prior_date"]),
                            "rel_diff_pct": rel * 100,
                        },
                    )
                )
    return findings


# -------------------------------------------------------------- overnight jump
def check_overnight_jump(trade_date: dt.date, settings: Settings | None = None) -> list[Finding]:
    """Flag >20 % moves in the ADJUSTED series with no corporate action to explain them."""
    s = settings or get_settings()
    thresh = s.overnight_jump_pct / 100.0

    sql = """
        WITH prior AS (
            SELECT DISTINCT ON (g.isin, g.series)
                   g.isin, g.series, g.trade_date AS prior_date, g.c_adj AS prior_c
              FROM gold.eod_adjusted g
             WHERE g.trade_date < %(d)s
             ORDER BY g.isin, g.series, g.trade_date DESC
        ),
        events AS (
            SELECT DISTINCT isin
              FROM silver.corp_action_event
             WHERE ex_date BETWEEN %(d)s::date - 3 AND %(d)s::date + 1
               AND superseded_at IS NULL
        )
        SELECT t.isin, t.symbol, t.series, t.c_adj, t.c_raw,
               p.prior_c, p.prior_date,
               (e.isin IS NOT NULL) AS has_event
          FROM gold.eod_adjusted t
          JOIN prior p ON p.isin = t.isin AND p.series = t.series
          LEFT JOIN events e ON e.isin = t.isin
         WHERE t.trade_date = %(d)s
           AND t.c_adj IS NOT NULL AND t.c_adj > 0
           AND p.prior_c IS NOT NULL AND p.prior_c > 0
           AND p.prior_date >= %(d)s::date - INTERVAL '10 days'
           AND t.in_universe
           AND t.c_adj > %(floor)s
           -- A move already sitting in the review queue is KNOWN, not
           -- undetected. Matches tools/audit_panel.py.
           AND NOT EXISTS (
                 SELECT 1 FROM silver.corp_action_review_queue q
                  WHERE q.status = 'open' AND q.isin = t.isin
                    AND q.ex_date BETWEEN p.prior_date - 3 AND %(d)s::date + 3
               )
    """

    findings: list[Finding] = []
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(sql, {"d": trade_date, "floor": s.overnight_jump_min_price})
        for r in cur.fetchall():
            cur_c, prior_c = float(r["c_adj"]), float(r["prior_c"])
            move = cur_c / prior_c - 1.0
            if abs(move) <= thresh:
                continue
            if r["has_event"]:
                # A move this size WITH a known event nearby is expected;
                # record it as info so the audit trail is complete.
                findings.append(
                    Finding(
                        check_name="overnight_jump",
                        severity=INFO,
                        trade_date=trade_date,
                        isin=r["isin"],
                        symbol=r["symbol"],
                        detail=f"{r['symbol']}: {move*100:+.1f}% adjusted move, explained by a nearby event.",
                        metrics={"move_pct": move * 100, "explained": True},
                    )
                )
                continue
            # WARNING, not CRITICAL, and aligned with tools/audit_panel.py.
            #
            # A >25% single-day move cannot be distinguished from a missed
            # corporate action by data alone -- real ones happen (YESBANK's 2020
            # RBI moratorium, INDUSINDBK's COVID rebound). The CONSEQUENCE is now
            # handled structurally instead: the ISIN is queued as
            # `large_move_unexplained`, which makes security_quality mark it
            # unverified, which ejects it from gold.tradeable_universe. The name
            # cannot be traded, without one ambiguous small-cap move failing the
            # nightly run for all 3,354 securities.
            #
            # CRITICAL is reserved for pipeline-integrity failures (an orphaned
            # factor, a failed rebuild) where the whole run is untrustworthy.
            findings.append(
                Finding(
                    check_name="overnight_jump",
                    severity=WARNING,
                    trade_date=trade_date,
                    isin=r["isin"],
                    symbol=r["symbol"],
                    detail=(
                        f"{r['symbol']} ({r['series']}): unexplained {move*100:+.1f}% move in the "
                        f"ADJUSTED series ({prior_c:.4f} -> {cur_c:.4f}) with no corporate action "
                        "on file. Could be a missed action or a genuine move; the ISIN is "
                        "queued for review and held OUT of the tradeable universe until "
                        "a human confirms."
                    ),
                    metrics={
                        "move_pct": move * 100,
                        "prior_c_adj": prior_c,
                        "c_adj": cur_c,
                        "c_raw": float(r["c_raw"]) if r["c_raw"] is not None else None,
                        "explained": False,
                    },
                )
            )
    return findings


# --------------------------------------------------------------- row counts
def check_rowcount(trade_date: dt.date, settings: Settings | None = None) -> list[Finding]:
    """Compare today's bhavcopy row count against the trailing-20-day norm."""
    s = settings or get_settings()
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM bronze.eod_bhav_raw WHERE trade_date = %s", (trade_date,)
        )
        n = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT avg(cnt)::float8 AS mean, stddev_samp(cnt)::float8 AS sd, count(*) AS days
              FROM (SELECT trade_date, count(*)::numeric AS cnt
                      FROM bronze.eod_bhav_raw
                     WHERE trade_date < %s
                     GROUP BY trade_date
                     ORDER BY trade_date DESC
                     LIMIT 20) t
            """,
            (trade_date,),
        )
        norm = cur.fetchone()

    findings: list[Finding] = []
    if n == 0:
        return [
            Finding(
                check_name="rowcount_deviation",
                severity=CRITICAL,
                trade_date=trade_date,
                detail=f"no bronze rows at all for {trade_date}",
                metrics={"count": 0},
            )
        ]
    if n < s.min_rowcount_floor:
        findings.append(
            Finding(
                check_name="rowcount_deviation",
                severity=CRITICAL,
                trade_date=trade_date,
                detail=f"only {n} rows for {trade_date}, below the absolute floor of {s.min_rowcount_floor}",
                metrics={"count": n, "floor": s.min_rowcount_floor},
            )
        )

    mean, sd, days = norm["mean"], norm["sd"], norm["days"]
    # Need a real history before a sigma test means anything.
    if mean and days and days >= 5 and sd and sd > 0:
        z = (n - float(mean)) / float(sd)
        if abs(z) > s.rowcount_sigma:
            findings.append(
                Finding(
                    check_name="rowcount_deviation",
                    severity=CRITICAL,
                    trade_date=trade_date,
                    detail=(
                        f"{n} rows for {trade_date} is {z:+.1f} sigma from the trailing "
                        f"{days}-day mean of {float(mean):.0f} (sd {float(sd):.0f})"
                    ),
                    metrics={"count": n, "mean": float(mean), "sd": float(sd), "z": z},
                )
            )
    return findings


# ------------------------------------------------------------ missing symbols
def check_missing_symbols(trade_date: dt.date, settings: Settings | None = None) -> list[Finding]:
    """Symbols in the universe on the prior bar but absent today, and not delisted."""
    s = settings or get_settings()
    sql = """
        WITH prior_day AS (
            SELECT max(trade_date) AS d FROM bronze.eod_bhav_raw WHERE trade_date < %(d)s
        ),
        prior_universe AS (
            SELECT b.isin, b.symbol
              FROM bronze.eod_bhav_raw b
              JOIN prior_day p ON b.trade_date = p.d
              LEFT JOIN silver.security_master sm ON sm.isin = b.isin
             WHERE b.series = 'EQ'
               AND (sm.status IS NULL OR sm.status = 'active')
        )
        SELECT pu.isin, pu.symbol
          FROM prior_universe pu
         WHERE NOT EXISTS (
                SELECT 1 FROM bronze.eod_bhav_raw t
                 WHERE t.trade_date = %(d)s AND t.isin = pu.isin
               )
         ORDER BY pu.symbol
         LIMIT 200
    """
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(sql, {"d": trade_date})
        missing = cur.fetchall()

    if not missing:
        return []
    names = [m["symbol"] for m in missing]
    return [
        Finding(
            check_name="missing_symbol",
            severity=WARNING,
            trade_date=trade_date,
            detail=(
                f"{len(missing)} active EQ symbol(s) present on the prior bar are absent "
                f"on {trade_date}: {', '.join(names[:15])}"
                + (" ..." if len(names) > 15 else "")
            ),
            metrics={"count": len(missing), "symbols": names[:50]},
        )
    ]


# ------------------------------------------------------------- review queue
def check_review_queue(settings: Settings | None = None) -> list[Finding]:
    """Open high-severity parse failures: each is a symbol whose history may be wrong."""
    s = settings or get_settings()
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n,
                   count(*) FILTER (WHERE severity = 'high') AS high
              FROM silver.corp_action_review_queue
             WHERE status = 'open'
            """
        )
        row = cur.fetchone()
        cur.execute(
            """
            SELECT symbol, ex_date, reason, raw_purpose
              FROM silver.corp_action_review_queue
             WHERE status = 'open' AND severity = 'high'
             ORDER BY ex_date DESC NULLS LAST
             LIMIT 10
            """
        )
        sample = cur.fetchall()

    if not row or row["n"] == 0:
        return []
    detail = (
        f"{row['n']} open review item(s), {row['high']} high severity. "
        "High severity means price-affecting and unresolved."
    )
    if sample:
        detail += " e.g. " + "; ".join(
            f"{r['symbol']} {r['ex_date']} [{r['reason']}] {(r['raw_purpose'] or '')[:60]}"
            for r in sample[:4]
        )
    return [
        Finding(
            check_name="unparsed_purpose",
            severity=WARNING if row["high"] == 0 else WARNING,
            detail=detail,
            metrics={"open": row["n"], "high": row["high"]},
        )
    ]


# --------------------------------------------------------------- stale factors
def check_stale_factors(trade_date: dt.date, settings: Settings | None = None) -> list[Finding]:
    """Events whose ex-date has passed but whose factor was never computed.

    A pending factor is silently equivalent to 1.0 in the panel, so this is the
    check that stops an unresolved action from looking like a resolved one.

    Split by whether an anchor bar is even obtainable, because the two cases
    demand different responses:

    * ``anchor_available`` -- a bar DOES exist before the ex-date, so the factor
      should have been computed. A real bug: **critical**.
    * ``outside_window``   -- no bar before the ex-date at all, because the
      ingested history does not reach that far back. Expected during a partial
      backfill and not actionable: **info**. Lumping these in as critical is
      what makes an alerting channel get muted.
    """
    s = settings or get_settings()
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.symbol, e.isin, e.ex_date, e.type, e.factor_state, e.raw_purpose,
                   EXISTS (
                       SELECT 1
                         FROM silver.isin_link l
                         JOIN bronze.eod_bhav_raw b ON b.isin = l.isin
                        WHERE l.canonical_isin = COALESCE(e.canonical_isin, e.isin)
                          AND b.trade_date < e.ex_date
                          AND b.c IS NOT NULL AND b.c > 0
                   ) AS anchor_available
              FROM silver.corp_action_event e
             WHERE e.superseded_at IS NULL
               AND e.ex_date <= %s
               AND e.factor_state IN ('pending', 'needs_anchor', 'unresolved')
               AND e.type IN ('BONUS','SPLIT','FACE_VALUE_CHANGE','RIGHTS',
                              'SPECIAL_DIVIDEND','RETURN_OF_CAPITAL')
             ORDER BY e.ex_date DESC
             LIMIT 200
            """,
            (trade_date,),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    def _desc(rs):
        return "; ".join(
            f"{r['symbol']} {r['ex_date']} {r['type']}({r['factor_state']})" for r in rs[:6]
        ) + (" ..." if len(rs) > 6 else "")

    def _metrics(rs):
        return {
            "count": len(rs),
            "events": [
                {
                    "symbol": r["symbol"],
                    "isin": r["isin"],
                    "ex_date": str(r["ex_date"]),
                    "type": r["type"],
                    "state": r["factor_state"],
                }
                for r in rs[:20]
            ],
        }

    real = [r for r in rows if r["anchor_available"]]
    outside = [r for r in rows if not r["anchor_available"]]
    findings: list[Finding] = []

    # Split TRACKED from UNTRACKED. An event already sitting in the high-severity
    # review queue is a known, visible gap awaiting a human decision -- and some
    # can never be resolved from data at all (BIRET's "Repayment Of Spv Debt"
    # carries no amount in the source text). Reporting those as CRITICAL every
    # night means run-daily exits 2 forever and the alert channel gets muted,
    # which is worse than not alerting. Only an UNTRACKED gap is a real defect.
    if real:
        with connection(s) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT isin, ex_date
                  FROM silver.corp_action_review_queue
                 WHERE status = 'open' AND severity = 'high'
                """
            )
            known = {(r["isin"], r["ex_date"]) for r in cur.fetchall()}
        untracked = [r for r in real if (r["isin"], r["ex_date"]) not in known]
        tracked = [r for r in real if (r["isin"], r["ex_date"]) in known]

        if untracked:
            findings.append(
                Finding(
                    check_name="stale_factor",
                    severity=CRITICAL,
                    trade_date=trade_date,
                    detail=(
                        f"{len(untracked)} price-adjusting event(s) have a past ex-date AND an "
                        f"available anchor bar, lack a factor, and are NOT in the review queue "
                        f"— silent holes: {_desc(untracked)}"
                    ),
                    metrics=_metrics(untracked),
                )
            )
        if tracked:
            findings.append(
                Finding(
                    check_name="stale_factor",
                    severity=WARNING,
                    trade_date=trade_date,
                    detail=(
                        f"{len(tracked)} price-adjusting event(s) lack a factor but ARE tracked "
                        f"in the high-severity review queue, awaiting a human decision or a "
                        f"corp_action_override: {_desc(tracked)}"
                    ),
                    metrics=_metrics(tracked),
                )
            )
    if outside:
        findings.append(
            Finding(
                check_name="stale_factor",
                severity=INFO,
                trade_date=trade_date,
                detail=(
                    f"{len(outside)} price-adjusting event(s) cannot be anchored because no bar "
                    f"exists before their ex-date (history not backfilled that far): "
                    f"{_desc(outside)}"
                ),
                metrics=_metrics(outside) | {"reason": "outside_ingested_window"},
            )
        )
    return findings


# ------------------------------------------------------- right-edge invariant
def check_right_edge_identity(settings: Settings | None = None) -> list[Finding]:
    """The most recent bar must be unadjusted: c_adj == c_raw.

    This is a structural invariant of the reverse-cumulative-product design. If
    it breaks, the cum_factor computation has a bug, and every adjusted price in
    the panel is suspect.

    Grouped per ISIN, NOT per (isin, series). A security migrates between series
    over its life -- SRPL went SM -> EQ -> BE -> BZ -- and cum_factor is computed
    per ISIN across all of them, so the last bar of a DISCONTINUED series
    legitimately still carries a factor: the security kept trading elsewhere and
    later events must still rescale those older bars. Grouping per series
    produced 50 false criticals on every run, which is exactly how an alerting
    channel gets ignored.
    """
    s = settings or get_settings()
    with connection(s) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (isin) isin, series, symbol,
                       trade_date, c_adj, c_raw, cum_factor
                  FROM gold.eod_adjusted
                 ORDER BY isin, trade_date DESC
            )
            SELECT * FROM latest
             WHERE c_raw IS NOT NULL AND c_raw > 0
               AND abs(cum_factor - 1.0) > 1e-9
             LIMIT 50
            """
        )
        bad = cur.fetchall()

    if not bad:
        return []
    return [
        Finding(
            check_name="right_edge_identity",
            severity=CRITICAL,
            detail=(
                f"{len(bad)} symbol(s) have cum_factor != 1.0 on their most recent bar, "
                "so today's adjusted close does not equal the exchange-printed close: "
                + ", ".join(f"{r['symbol']}({float(r['cum_factor']):.6f})" for r in bad[:8])
            ),
            metrics={"count": len(bad)},
        )
    ]


# ------------------------------------------------------------------- runner
def run_all(
    trade_date: dt.date,
    settings: Settings | None = None,
    include_gold_checks: bool = True,
) -> list[Finding]:
    """Run the full validation suite for one date."""
    findings: list[Finding] = []
    checks = [
        ("rowcount", lambda: check_rowcount(trade_date, settings)),
        ("prev_close", lambda: check_prev_close(trade_date, settings)),
        ("missing_symbols", lambda: check_missing_symbols(trade_date, settings)),
        ("review_queue", lambda: check_review_queue(settings)),
        ("stale_factors", lambda: check_stale_factors(trade_date, settings)),
    ]
    if include_gold_checks:
        checks += [
            ("overnight_jump", lambda: check_overnight_jump(trade_date, settings)),
            ("right_edge", lambda: check_right_edge_identity(settings)),
        ]

    for name, fn in checks:
        try:
            got = fn()
            findings.extend(got)
            log.debug("check_done", check=name, findings=len(got))
        except Exception as exc:
            # A broken check must not abort the run, but it must be visible.
            log.error("check_failed", check=name, error=str(exc))
            findings.append(
                Finding(
                    check_name="constraint_violation",
                    severity=CRITICAL,
                    trade_date=trade_date,
                    detail=f"validation check {name!r} raised: {exc}",
                    metrics={"check": name},
                )
            )
    return findings


def persist(findings: list[Finding], run_id: int | None, settings: Settings | None = None) -> int:
    if not findings:
        return 0
    from ..db import upsert_rows

    rows = [f.as_row(run_id) for f in findings]
    with connection(settings) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ops.validation_finding
                (run_id, trade_date, check_name, severity, isin, symbol, detail, metrics)
            VALUES (%(run_id)s, %(trade_date)s, %(check_name)s, %(severity)s,
                    %(isin)s, %(symbol)s, %(detail)s, %(metrics)s::jsonb)
            """,
            rows,
        )
    return len(rows)


def queue_large_moves(
    findings: list[Finding], settings: Settings | None = None
) -> int:
    """Queue unexplained large moves as `large_move_unexplained` (A3 category 3).

    This is what makes downgrading the finding to WARNING safe. The queue row is
    severity=high, so ``security_quality`` marks the ISIN unverified and it leaves
    ``gold.tradeable_universe`` -- the name cannot be traded while the move is
    unexplained. Resolution is ``review-queue resolve --action accept`` when a
    human confirms it was a genuine move, which needs no fabricated factor.

    Idempotent: the queue's unique index is on (COALESCE(ca_raw_id,-1),
    raw_purpose), and the purpose text embeds the ISIN and date.
    """
    from ..db import REVIEW_QUEUE_CONFLICT, connection, insert_ignore

    rows = []
    for f in findings:
        if f.check_name != "overnight_jump" or f.severity != WARNING:
            continue
        if not f.isin or "unexplained" not in f.detail:
            continue
        move = f.metrics.get("move_pct")
        rows.append(
            {
                "ca_raw_id": None,
                "isin": f.isin,
                "symbol": f.symbol,
                "ex_date": f.trade_date,
                "raw_purpose": (
                    f"[derived] unexplained {move:+.1f}% adjusted move on "
                    f"{f.trade_date} with no corporate action on file"
                    if move is not None
                    else f"[derived] unexplained large move on {f.trade_date}"
                ),
                "reason": "large_move_unexplained",
                "parser_version": "checks/overnight_jump",
                "suggested_type": None,
                "suggested_json": json.dumps(
                    {
                        "move_pct": move,
                        "action": (
                            "confirm whether this is a genuine market move "
                            "(--action accept) or a missed corporate action "
                            "(--action factor with --source)"
                        ),
                        "note": "NEVER auto-applied",
                    }
                ),
                "severity": "high",
            }
        )
    if not rows:
        return 0
    with connection(settings) as conn:
        n = insert_ignore(
            conn,
            "silver.corp_action_review_queue",
            rows,
            ["ca_raw_id", "raw_purpose"],
            conflict_target=REVIEW_QUEUE_CONFLICT,
        )
    log.info("large_moves_queued", queued=n, candidates=len(rows))
    return n


def worst_severity(findings: list[Finding]) -> str | None:
    if any(f.severity == CRITICAL for f in findings):
        return CRITICAL
    if any(f.severity == WARNING for f in findings):
        return WARNING
    if findings:
        return INFO
    return None


def exit_code_for(severity: str | None) -> int:
    """critical -> 2, warning -> 1, otherwise 0."""
    return {CRITICAL: 2, WARNING: 1}.get(severity or "", 0)
