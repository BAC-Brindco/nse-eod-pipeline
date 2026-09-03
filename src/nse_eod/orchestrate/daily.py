"""The `run-daily` state machine (intended ~19:00 IST) and `backfill`.

Idempotency rests on four things:
  1. natural-key upserts everywhere,
  2. content hashing, so an unchanged source file writes nothing,
  3. ``ops.run_watermark``, so a completed day is skipped, and
  4. gold being a pure function of bronze + the factor table.

The one case where a re-run is legitimately NOT a no-op is an NSE restatement:
the file content hash changes, work re-triggers, and the run summary reports
``bhav_rows_restated``.
"""

from __future__ import annotations

import datetime as dt
import traceback

from ..config import Settings, get_settings
from ..db import (
    close_pool,
    finish_run,
    get_watermark,
    is_already_done,
    set_watermark,
    start_run,
)
from ..logging_setup import RunSummary, bind_run, get_logger, new_run_uid
from ..sources.bhavcopy import BhavcopyUnavailable
from ..sources.http import NSESession
from ..transform.adjusted import materialize
from ..transform.anchors import compute_pending_factors
from ..transform import isin_link, quality
from ..validate import factor_guard
from ..validate import alerts
from ..validate import checks as V
from .ingest import (
    derive_calendar_from_observation,
    ingest_calendar,
    ingest_corp_actions,
    ingest_eod,
    ingest_security_master,
    is_trading_day,
)

log = get_logger(__name__)

CMD_DAILY = "run-daily"


class DailyResult:
    def __init__(self, exit_code: int, status: str, summary: RunSummary):
        self.exit_code = exit_code
        self.status = status
        self.summary = summary


def run_daily(
    trade_date: dt.date | None = None,
    settings: Settings | None = None,
    session: NSESession | None = None,
    force: bool = False,
    skip_corp_actions: bool = False,
    skip_gold: bool = False,
) -> DailyResult:
    """Execute one daily cycle. Returns the exit code and run summary."""
    s = settings or get_settings()
    d = trade_date or dt.date.today()
    run_uid = new_run_uid()
    bind_run(run_uid, CMD_DAILY)
    summary = RunSummary(command=CMD_DAILY, run_uid=run_uid, target_date=d)

    log.info("run_daily_start", date=str(d), force=force)

    # ---- 1. calendar gate -------------------------------------------------
    # Ensure the calendar covers this date before consulting it.
    owns_session = session is None
    sess = session or NSESession(s)
    try:
        ingest_calendar(d - dt.timedelta(days=7), d + dt.timedelta(days=7), sess, s)
    except Exception as exc:
        log.warning("calendar_refresh_failed", error=str(exc))

    trading, reason = is_trading_day(d, s)
    if not trading and not force:
        run_id = start_run(CMD_DAILY, run_uid, d)
        summary.note(f"skipped: {reason}")
        summary.log(log)
        finish_run(run_id, "skipped", 0, summary)
        set_watermark(CMD_DAILY, d, "skipped", run_id)
        log.info("run_daily_skipped", date=str(d), reason=reason)
        if owns_session:
            sess.close()
        return DailyResult(0, "skipped", summary)

    # ---- 2. watermark ------------------------------------------------------
    wm = get_watermark(CMD_DAILY, d)
    if wm and wm["status"] == "success" and not force:
        log.info(
            "run_daily_watermark_hit",
            date=str(d),
            note="will re-check the source hash before deciding it is a no-op",
        )

    run_id = start_run(CMD_DAILY, run_uid, d)
    findings: list[V.Finding] = []
    status = "success"
    exit_code = 0

    try:
        # ---- 3. bronze bars ------------------------------------------------
        try:
            res = ingest_eod(d, sess, s)
            summary.bhav_rows_ingested = res["rows"]
            summary.bhav_rows_restated = res["restated"]
            summary.extra["source_format"] = res["source_format"]
            content_hash = res["content_hash"]

            if not force and is_already_done(CMD_DAILY, d, content_hash):
                summary.note("no-op: identical source content already ingested")
                log.info("run_daily_noop", date=str(d))
        except BhavcopyUnavailable as exc:
            # No bhavcopy on a day the calendar called tradeable: most often a
            # holiday the master mislabelled. Record it, do not fail the run.
            summary.note(f"no bhavcopy: {exc}")
            findings.append(
                V.Finding(
                    check_name="rowcount_deviation",
                    severity=V.WARNING,
                    trade_date=d,
                    detail=f"no bhavcopy published for {d} though the calendar marked it tradeable ({reason})",
                )
            )
            content_hash = None
            log.warning("bhavcopy_unavailable", date=str(d), error=str(exc))

        # ---- 3b. index closes (India VIX + Nifty 50) ------------------------
        # Same session's all-index file. Kept NON-FATAL on purpose: the equity panel
        # is the pipeline's product and must not fail to build because a secondary
        # index file was late. A missing day surfaces as a warning and the next run
        # picks it up, because ingest_range skips dates already stored.
        try:
            from ..transform.indices import ingest_range as _ingest_indices

            idx = _ingest_indices(d, d, s, skip_existing=False)
            summary.extra["index_rows"] = idx["rows"]
            if idx["rows"] == 0 and idx["days_absent"]:
                summary.note("no index close file for this date")
            log.info("indices_ingested", **idx)
        except Exception as exc:  # noqa: BLE001
            summary.note(f"index ingest failed (non-fatal): {exc}")
            log.warning("index_ingest_failed", error=str(exc)[:200])

        # ---- 4. security master -------------------------------------------
        try:
            ingest_security_master(sess, s)
        except Exception as exc:
            log.warning("security_master_failed", error=str(exc))
            summary.note(f"security master refresh failed: {exc}")

        # ---- 5. corp actions ----------------------------------------------
        if not skip_corp_actions:
            try:
                ca = ingest_corp_actions(
                    d - dt.timedelta(days=s.ca_lookback_days),
                    d + dt.timedelta(days=s.ca_lookahead_days),
                    sess,
                    s,
                    scrape_date=d,
                )
                summary.ca_rows_ingested = ca["ca_rows_ingested"]
                summary.ca_events_parsed = ca["events_parsed"]
                summary.ca_flagged_for_review = ca["flagged_for_review"]
            except Exception as exc:
                log.error("corp_actions_failed", error=str(exc))
                summary.note(f"corp action ingest failed: {exc}")
                findings.append(
                    V.Finding(
                        check_name="constraint_violation",
                        severity=V.CRITICAL,
                        trade_date=d,
                        detail=f"corporate action ingest failed: {exc}",
                    )
                )

        # ---- 6. ISIN linkage, then factors ---------------------------------
        # Must precede factor computation: a face-value split issues a NEW ISIN,
        # and the corp-action feed often reports a superseded one. Without the
        # link the event finds no bars, gets no anchor, and the fake ex-date gap
        # survives into gold.
        try:
            lstats = isin_link.refresh(s)
            summary.extra["isin_link"] = lstats
        except Exception as exc:
            log.warning("isin_link_failed", error=str(exc))
            summary.note(f"isin linkage failed: {exc}")

        fstats = compute_pending_factors()
        summary.factors_computed = fstats.get("computed", 0)
        summary.extra["factor_states"] = fstats

        # ---- 6b. ZERO-MULTIPLIER GUARD (A1) --------------------------------
        # A factor filed against an ISIN with no bars before its ex-date
        # multiplies ZERO rows: the price is silently never adjusted and every
        # other check still passes. That is how HDFC Bank's 2025 Bonus 1:1 left
        # the largest private bank unadjusted by 2x with a green board.
        #
        # Scoped to events touched in the last 2 days, matching the gold rebuild
        # window below: this asks "did TONIGHT orphan anything?" rather than
        # re-reporting the historical backlog every night. The full backlog is
        # covered by `nse-eod assert-factors --sweep`.
        try:
            guard_findings, guard_exit = factor_guard.gate(
                s, since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
            )
            new_orphans = [f for f in guard_findings if f.is_actionable]
            summary.extra["factor_guard"] = {
                "flagged": len(guard_findings),
                "orphaned": len(new_orphans),
            }
            if new_orphans:
                # Fatal: an unadjusted security must never reach the trading
                # layer silently. pre_history findings do NOT fail the run.
                guard_orphan_exit = 2

                # FAIL CLOSED. Declining to re-admit is not enough: gold has
                # already been rewritten with the unadjusted prices, so the
                # affected names must be EJECTED from the universe now, before
                # this run aborts. Otherwise they sit there still marked
                # verified while their prices are wrong.
                bad_isins = sorted(
                    {f.canonical_isin or f.isin for f in new_orphans if (f.canonical_isin or f.isin)}
                )
                # Quarantine the security's OTHER identities too: the orphan is
                # by definition filed on the wrong one, so the ISIN actually
                # carrying the bad prices may be a different chain member.
                try:
                    from ..db import connection as _conn

                    with _conn(s) as c, c.cursor() as cur:
                        cur.execute(
                            """
                            SELECT DISTINCT COALESCE(l.canonical_isin, b.isin) AS isin
                              FROM bronze.eod_bhav_raw b
                              LEFT JOIN silver.isin_link l ON l.isin = b.isin
                             WHERE b.symbol = ANY(%s)
                            """,
                            ([f.symbol for f in new_orphans if f.symbol],),
                        )
                        bad_isins = sorted(set(bad_isins) | {r["isin"] for r in cur.fetchall()})
                    qstats = quality.quarantine_isins(
                        bad_isins,
                        note=(
                            "quarantined by the zero-multiplier guard: an orphaned "
                            "factor means this security's adjusted history is wrong"
                        ),
                        settings=s,
                    )
                    summary.extra["quarantined"] = qstats
                except Exception as exc:
                    log.error("quarantine_failed", error=str(exc))
                    summary.note(f"QUARANTINE FAILED for {bad_isins}: {exc}")
                for f in new_orphans:
                    findings.append(
                        V.Finding(
                            check_name="orphaned_factor",
                            severity=V.CRITICAL,
                            trade_date=d,
                            isin=f.canonical_isin or f.isin,
                            symbol=f.symbol,
                            detail=f.describe(),
                            metrics={
                                "event_id": f.event_id,
                                "filed_isin": f.isin,
                                "resolved_isin": f.canonical_isin,
                                "factor_price": f.factor_price,
                                "bars_by_symbol": f.bars_by_symbol,
                            },
                        )
                    )
            else:
                guard_orphan_exit = 0
        except Exception as exc:
            log.error("factor_guard_failed", error=str(exc))
            summary.note(f"factor guard failed: {exc}")
            findings.append(
                V.Finding(
                    check_name="constraint_violation",
                    severity=V.CRITICAL,
                    trade_date=d,
                    detail=f"zero-multiplier guard could not run: {exc}",
                )
            )
            guard_orphan_exit = 2

        # ---- 7. gold -------------------------------------------------------
        if not skip_gold:
            # Rebuild every ISIN that traded today, PLUS every ISIN whose
            # factors changed -- a new ex-date rescales that symbol's whole
            # history, so a day-scoped rebuild would leave the past stale.
            from ..db import connection

            with connection(s) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT COALESCE(l.canonical_isin, b.isin) AS isin
                      FROM bronze.eod_bhav_raw b
                      LEFT JOIN silver.isin_link l ON l.isin = b.isin
                     WHERE b.trade_date = %s
                    UNION
                    SELECT DISTINCT COALESCE(e.canonical_isin, e.isin) AS isin
                      FROM silver.corp_action_event e
                     WHERE e.superseded_at IS NULL
                       AND e.updated_at >= now() - INTERVAL '2 days'
                    """,
                    (d,),
                )
                isins = [r["isin"] for r in cur.fetchall()]

            gstats = materialize(isins, settings=s)
            summary.gold_isins_rebuilt = gstats["isins"]
            summary.gold_rows_written = gstats["rows"]

        # ---- 8. validation -------------------------------------------------
        findings.extend(V.run_all(d, s, include_gold_checks=not skip_gold))
        V.persist(findings, run_id, s)

        # Queue unexplained large moves BEFORE quality is recomputed, so the
        # affected ISINs are marked unverified and leave the universe on this
        # same run. This is what makes the WARNING severity safe: the name is
        # held out of trading even though the run itself does not fail.
        try:
            summary.extra["large_moves_queued"] = V.queue_large_moves(findings, s)
        except Exception as exc:
            log.error("queue_large_moves_failed", error=str(exc))
            summary.note(f"could not queue large moves: {exc}")
        severity = V.worst_severity(findings)
        # The guard's verdict is folded in explicitly rather than relying on the
        # finding's severity alone, so a future change to severity->exit mapping
        # cannot quietly stop an orphan from failing the run.
        exit_code = max(V.exit_code_for(severity), guard_orphan_exit)
        summary.alerts_raised = sum(
            1 for f in findings if f.severity in (V.CRITICAL, V.WARNING)
        )

        if summary.alerts_raised:
            alerts.send(findings, CMD_DAILY, d, run_uid, summary.counters(), s)

        # ---- 9. VERIFIED UNIVERSE (A2) -------------------------------------
        # Rebuilt ONLY when the checks pass. gold.tradeable_universe is a table,
        # not a view, precisely so a failed run leaves the previous known-good
        # universe in place instead of the gate silently widening to include
        # prices nothing has verified. Unverified names are ABSENT, not flagged.
        if not skip_gold:
            if exit_code < 2:
                try:
                    qstats = quality.refresh(s)
                    summary.extra["quality"] = qstats["quality"]
                    summary.extra["universe"] = qstats["universe"]
                    log.info(
                        "universe_gate_rebuilt",
                        verified=qstats["quality"]["verified"],
                        review_open=qstats["quality"]["review_open"],
                        universe_rows=qstats["universe"]["universe_rows"],
                    )
                except Exception as exc:
                    log.error("universe_gate_failed", error=str(exc))
                    summary.note(f"universe gate rebuild failed: {exc}")
                    findings.append(
                        V.Finding(
                            check_name="constraint_violation",
                            severity=V.CRITICAL,
                            trade_date=d,
                            detail=f"tradeable_universe rebuild failed: {exc}",
                        )
                    )
                    V.persist(findings[-1:], run_id, s)
                    exit_code = max(exit_code, 2)
            else:
                summary.note(
                    "universe gate NOT rebuilt: checks did not pass (exit>=2). "
                    "The previous verified universe is intentionally left in place."
                )
                log.warning("universe_gate_skipped", exit_code=exit_code)

        status = {0: "success", 1: "warning", 2: "warning"}.get(exit_code, "warning")

        set_watermark(
            CMD_DAILY,
            d,
            "success" if exit_code == 0 else "warning",
            run_id,
            content_hash,
        )

    except Exception as exc:
        tb = traceback.format_exc()
        log.error("run_daily_failed", error=str(exc), traceback=tb)
        summary.note(f"fatal: {exc}")
        finish_run(run_id, "failed", 3, summary, error_text=tb[:4000])
        set_watermark(CMD_DAILY, d, "failed", run_id)
        alerts.send(
            [
                V.Finding(
                    check_name="constraint_violation",
                    severity=V.CRITICAL,
                    trade_date=d,
                    detail=f"run-daily crashed: {exc}",
                )
            ],
            CMD_DAILY,
            d,
            run_uid,
            summary.counters(),
            s,
        )
        if owns_session:
            sess.close()
        return DailyResult(3, "failed", summary)

    summary.log(log)
    finish_run(run_id, status, exit_code, summary)
    log.info("run_daily_done", date=str(d), status=status, exit_code=exit_code)

    if owns_session:
        sess.close()
    return DailyResult(exit_code, status, summary)


# ------------------------------------------------------------------ backfill
def backfill(
    start: dt.date,
    end: dt.date,
    settings: Settings | None = None,
    session: NSESession | None = None,
    skip_corp_actions: bool = False,
    rebuild_gold: bool = True,
) -> RunSummary:
    """Ingest a date range, then rebuild gold once at the end.

    Gold is deliberately materialized ONCE after all bars and factors are in
    place, rather than per day: a day-by-day rebuild would redo the same ISIN
    histories thousands of times for an identical result.
    """
    s = settings or get_settings()
    run_uid = new_run_uid()
    bind_run(run_uid, "backfill")
    summary = RunSummary(command="backfill", run_uid=run_uid, target_date=end)
    run_id = start_run("backfill", run_uid, end)

    owns_session = session is None
    sess = session or NSESession(s)

    log.info("backfill_start", start=str(start), end=str(end))

    try:
        ingest_calendar(start, end + dt.timedelta(days=7), sess, s)

        # 1. corp actions for the whole span first, so factors exist before gold.
        if not skip_corp_actions:
            try:
                ca = ingest_corp_actions(
                    start - dt.timedelta(days=30),
                    end + dt.timedelta(days=s.ca_lookahead_days),
                    sess,
                    s,
                    scrape_date=dt.date.today(),
                )
                summary.ca_rows_ingested = ca["ca_rows_ingested"]
                summary.ca_events_parsed = ca["events_parsed"]
                summary.ca_flagged_for_review = ca["flagged_for_review"]
            except Exception as exc:
                log.error("backfill_corp_actions_failed", error=str(exc))
                summary.note(f"corp actions failed: {exc}")

        # 2. bars, day by day, resumable.
        d = start
        days_done = 0
        days_skipped = 0
        while d <= end:
            # EVERY day is attempted -- no calendar rule is trusted.
            # Saturdays: 2020-02-01, 2020-11-14, 2024-01-20, 2024-03-02,
            # 2024-05-18, 2025-02-01 all published real bhavcopies.
            # Sundays too: 2026-02-01 (Union Budget) published 180 KB.
            # A 404 costs ~0.16 s; a wrongly skipped day is lost silently.
            if is_already_done("ingest-eod", d):
                days_skipped += 1
                d += dt.timedelta(days=1)
                continue
            try:
                res = ingest_eod(d, sess, s)
                summary.bhav_rows_ingested += res["rows"]
                summary.bhav_rows_restated += res["restated"]
                set_watermark("ingest-eod", d, "success", run_id, res["content_hash"])
                days_done += 1
            except BhavcopyUnavailable:
                # Holiday or out of range. Mark it so a resume does not retry.
                set_watermark("ingest-eod", d, "skipped", run_id)
                days_skipped += 1
            except Exception as exc:
                log.error("backfill_day_failed", date=str(d), error=str(exc))
                summary.note(f"{d}: {exc}")
                set_watermark("ingest-eod", d, "failed", run_id)
            d += dt.timedelta(days=1)

        summary.extra["days_ingested"] = days_done
        summary.extra["days_skipped"] = days_skipped

        # 3. calendar from evidence, master, factors, gold.
        # The holiday API only covers the current year, so historical holidays
        # must come from observation or the audit cannot distinguish a holiday
        # from a missing ingest.
        try:
            summary.extra["calendar"] = derive_calendar_from_observation(s)
        except Exception as exc:
            log.warning("backfill_calendar_derive_failed", error=str(exc))

        try:
            ingest_security_master(sess, s)
        except Exception as exc:
            log.warning("backfill_master_failed", error=str(exc))

        try:
            summary.extra["isin_link"] = isin_link.refresh(s)
        except Exception as exc:
            log.warning("backfill_isin_link_failed", error=str(exc))

        fstats = compute_pending_factors()
        summary.factors_computed = fstats.get("computed", 0)

        if rebuild_gold:
            gstats = materialize(None, settings=s)
            summary.gold_isins_rebuilt = gstats["isins"]
            summary.gold_rows_written = gstats["rows"]

        summary.log(log)
        finish_run(run_id, "success", 0, summary)
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("backfill_failed", error=str(exc))
        finish_run(run_id, "failed", 3, summary, error_text=tb[:4000])
        raise
    finally:
        if owns_session:
            sess.close()

    log.info("backfill_done", start=str(start), end=str(end))
    return summary
