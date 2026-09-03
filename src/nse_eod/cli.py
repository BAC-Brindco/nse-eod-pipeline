"""CLI entrypoints.

    nse-eod migrate
    nse-eod ingest-eod [--date YYYY-MM-DD]
    nse-eod ingest-corp-actions [--from ...] [--to ...]
    nse-eod rebuild-adjusted [--isin ...] [--all] [--as-of ...]
    nse-eod run-daily [--date ...] [--force]
    nse-eod backfill --from ... [--to ...]
    nse-eod review-queue [--severity high]
    nse-eod doctor

Exit codes: 0 clean, 1 warnings, 2 critical findings, 3 crash.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

import typer

from .config import PROJECT_ROOT, get_settings
from .logging_setup import bind_run, get_logger, new_run_uid, setup_logging

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="NSE corporate-action-adjusted EOD price pipeline.",
)
log = get_logger("cli")


def _parse_date(value: str | None) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


@app.callback()
def _root(
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG/INFO/WARNING/ERROR"),
    json_logs: bool = typer.Option(False, "--json-logs", help="JSON to console as well as file"),
) -> None:
    s = get_settings()
    setup_logging(s.log_dir, level=log_level, json_console=json_logs)


# ------------------------------------------------------------------- migrate
@app.command()
def migrate(
    migrations_dir: Optional[Path] = typer.Option(None, help="defaults to ./migrations"),
) -> None:
    """Apply all SQL migrations (idempotent)."""
    from .db import apply_migrations

    d = migrations_dir or (PROJECT_ROOT / "migrations")
    applied = apply_migrations(d)
    typer.echo(f"applied {len(applied)} migration(s): {', '.join(applied)}")


# ---------------------------------------------------------------- ingest-eod
@app.command("ingest-eod")
def ingest_eod_cmd(
    date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD (default: today)"),
    force: bool = typer.Option(False, "--force", help="ignore the watermark"),
) -> None:
    """Fetch and upsert one day of bars into bronze."""
    from .db import is_already_done, set_watermark
    from .orchestrate.ingest import ingest_eod
    from .sources.http import NSESession

    s = get_settings()
    d = _parse_date(date) or dt.date.today()
    bind_run(new_run_uid(), "ingest-eod")

    with NSESession(s) as sess:
        res = ingest_eod(d, sess, s)

    if not force and is_already_done("ingest-eod", d, res["content_hash"]):
        typer.echo(f"{d}: no-op (source content unchanged)")
    set_watermark("ingest-eod", d, "success", None, res["content_hash"])
    typer.echo(
        f"{d}: {res['rows']} rows ({res['source_format']}), "
        f"upserted {res['upserted']}, restated {res['restated']}"
    )


# --------------------------------------------------------- ingest-corp-actions
@app.command("ingest-indices")
def ingest_indices_cmd(
    date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD; default today"),
    from_: Optional[str] = typer.Option(None, "--from", help="YYYY-MM-DD range start"),
    to: Optional[str] = typer.Option(None, "--to", help="YYYY-MM-DD range end"),
    refetch: bool = typer.Option(
        False, "--refetch", help="re-fetch dates already stored (default: skip them)"
    ),
) -> None:
    """Ingest NSE's daily all-index close file: India VIX, Nifty 50 and ~163 others.

    One archive file per trading day. Weekends and holidays have no file, which is a
    non-trading-day signal rather than a failure. Resumable: dates already in
    silver.index_daily are skipped unless --refetch.
    """
    import datetime as _dt

    from .transform.indices import ingest_range

    s = get_settings()
    if from_ or to:
        start = _dt.date.fromisoformat(from_) if from_ else s.backfill_start
        end = _dt.date.fromisoformat(to) if to else _dt.date.today()
    else:
        d = _dt.date.fromisoformat(date) if date else _dt.date.today()
        start = end = d

    typer.echo(f"ingesting index closes {start} -> {end}")
    stats = ingest_range(start, end, s, skip_existing=not refetch)
    for k, v in stats.items():
        typer.echo(f"  {k:<14} {v}")
    if stats["errors"]:
        typer.secho(f"  {stats['errors']} day(s) failed; re-run to retry", fg="yellow")
        raise typer.Exit(1)


@app.command("ingest-corp-actions")
def ingest_corp_actions_cmd(
    from_date: Optional[str] = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to_date: Optional[str] = typer.Option(None, "--to", help="YYYY-MM-DD"),
) -> None:
    """Fetch corporate actions, parse them, and upsert the factor table."""
    from .orchestrate.ingest import ingest_corp_actions
    from .sources.http import NSESession

    s = get_settings()
    today = dt.date.today()
    a = _parse_date(from_date) or today - dt.timedelta(days=s.ca_lookback_days)
    b = _parse_date(to_date) or today + dt.timedelta(days=s.ca_lookahead_days)
    bind_run(new_run_uid(), "ingest-corp-actions")

    with NSESession(s) as sess:
        stats = ingest_corp_actions(a, b, sess, s, scrape_date=today)

    typer.echo(
        f"{a}..{b}: fetched {stats['ca_rows_fetched']}, bronze {stats['ca_rows_ingested']}, "
        f"events {stats['events_parsed']}, price-adjusting {stats['price_adjusting']}, "
        f"flagged {stats['flagged_for_review']}"
    )


# ------------------------------------------------------------ rebuild-adjusted
@app.command("rebuild-adjusted")
def rebuild_adjusted_cmd(
    isin: list[str] = typer.Option(None, "--isin", help="repeatable; omit with --all"),
    all_isins: bool = typer.Option(False, "--all", help="rebuild the entire panel"),
    as_of: Optional[str] = typer.Option(
        None, "--as-of", help="YYYY-MM-DD: reconstruct the panel as it looked then"
    ),
    recompute_factors: bool = typer.Option(True, "--recompute-factors/--no-recompute-factors"),
    reset_factors: bool = typer.Option(
        False,
        "--reset-factors",
        help="re-derive EVERY factor from scratch; use after changing factor/anchor logic",
    ),
) -> None:
    """Recompute cumulative factors and materialize the gold panel."""
    from .transform import isin_link
    from .transform.adjusted import materialize
    from .transform.anchors import compute_pending_factors, reset_factor_states

    s = get_settings()
    bind_run(new_run_uid(), "rebuild-adjusted")

    if not all_isins and not isin:
        raise typer.BadParameter("pass --all or at least one --isin")

    if reset_factors:
        n = reset_factor_states()
        typer.echo(f"reset {n:,} event(s) to 'pending' for full factor re-derivation")

    if recompute_factors:
        # Linkage first: a split-issued ISIN must be resolved before factors can
        # find their own price history.
        lstats = isin_link.refresh(s)
        typer.echo(f"isin links: {lstats['detect']}")
        typer.echo(f"event isins: {lstats['resolve']}")
        fstats = compute_pending_factors()
        typer.echo(f"factors: {fstats}")

    asof_dt = None
    if as_of:
        d = _parse_date(as_of)
        asof_dt = dt.datetime.combine(d, dt.time.max).replace(tzinfo=dt.timezone.utc)

    stats = materialize(None if all_isins else list(isin), as_of=asof_dt, settings=s)
    typer.echo(f"gold: {stats['isins']} isin(s), {stats['rows']} row(s) written")


# ----------------------------------------------------------------- run-daily
@app.command("run-daily")
def run_daily_cmd(
    date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD (default: today)"),
    force: bool = typer.Option(False, "--force", help="run even on a non-trading day"),
    skip_corp_actions: bool = typer.Option(False, "--skip-corp-actions"),
    skip_gold: bool = typer.Option(False, "--skip-gold"),
) -> None:
    """Full daily cycle. Exits 1 on warnings, 2 on critical findings."""
    from .orchestrate.daily import run_daily

    s = get_settings()
    res = run_daily(
        _parse_date(date),
        s,
        force=force,
        skip_corp_actions=skip_corp_actions,
        skip_gold=skip_gold,
    )
    typer.echo(f"status={res.status} exit={res.exit_code}")
    typer.echo(json.dumps(res.summary.counters(), indent=2))
    for n in res.summary.notes:
        typer.echo(f"  note: {n}")
    raise typer.Exit(res.exit_code)


# ------------------------------------------------------------------ backfill
@app.command()
def backfill(
    from_date: Optional[str] = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to_date: Optional[str] = typer.Option(None, "--to", help="YYYY-MM-DD (default: today)"),
    skip_corp_actions: bool = typer.Option(False, "--skip-corp-actions"),
    no_gold: bool = typer.Option(False, "--no-gold", help="ingest only, skip materialization"),
) -> None:
    """Backfill a date range from raw sources, then rebuild the panel."""
    from .orchestrate.daily import backfill as run_backfill

    s = get_settings()
    a = _parse_date(from_date) or s.backfill_start
    b = _parse_date(to_date) or dt.date.today()
    if a > b:
        raise typer.BadParameter(f"--from {a} is after --to {b}")

    summary = run_backfill(
        a, b, s, skip_corp_actions=skip_corp_actions, rebuild_gold=not no_gold
    )
    typer.echo(json.dumps(summary.counters(), indent=2))
    typer.echo(json.dumps(summary.extra, indent=2, default=str))


# ------------------------------------------------------------------- reparse
@app.command("reparse-corp-actions")
def reparse_corp_actions_cmd(
    only_unparsed: bool = typer.Option(
        False, "--only-unparsed", help="faster: only rows with an unparsed component"
    ),
) -> None:
    """Re-run the current parser over all stored raw announcements.

    Run this after ANY parser change. Bronze is immutable and complete, so silver
    is always rebuildable from it — a parser fix that is not reparsed applies
    only to future ingests and leaves history wrong.
    """
    from .orchestrate.ingest import reparse_corp_actions

    s = get_settings()
    bind_run(new_run_uid(), "reparse-corp-actions")
    stats = reparse_corp_actions(s, only_unparsed=only_unparsed)
    for k, v in stats.items():
        typer.echo(f"  {k:<20} {v:>8,}")
    if stats["events_changed"]:
        typer.echo(
            "\nfactors were reset to 'pending' for changed events — "
            "run `rebuild-adjusted --all` to apply them to the panel"
        )


# ----------------------------------------------------------------- calendar
@app.command("derive-calendar")
def derive_calendar_cmd() -> None:
    """Fill the historical trading calendar from observed data.

    NSE's holiday API only covers the current year, so historical holidays must
    be established from evidence: a weekday with bars traded; a weekday whose
    ingest was attempted and found nothing did not.
    """
    from .orchestrate.ingest import derive_calendar_from_observation

    s = get_settings()
    bind_run(new_run_uid(), "derive-calendar")
    stats = derive_calendar_from_observation(s)
    typer.echo(
        f"trading days: {stats['trading_days']:,} | "
        f"observed holidays: {stats['observed_holidays']:,}"
    )


# ---------------------------------------------------------------- link-isins
@app.command("link-isins")
def link_isins_cmd() -> None:
    """Detect ISIN changes (face-value splits) and resolve event ISINs."""
    from .db import fetch_all
    from .transform import isin_link

    s = get_settings()
    bind_run(new_run_uid(), "link-isins")
    stats = isin_link.refresh(s)
    typer.echo(f"detect : {stats['detect']}")
    typer.echo(f"resolve: {stats['resolve']}")
    typer.echo(f"history: {stats['history_rows']} security_master row(s) updated")

    chains = fetch_all("SELECT * FROM ops.v_isin_chains ORDER BY symbol")
    if chains:
        typer.echo("")
        typer.echo(f"{len(chains)} multi-ISIN entit(ies):")
        for c in chains:
            typer.echo(f"  {(c['symbol'] or '?'):<12} {c['chain']}")


# ---------------------------------------------------------- assert-factors (A1)
@app.command("assert-factors")
def assert_factors_cmd(
    sweep: bool = typer.Option(
        False, "--sweep", help="one-time pass over ALL events, queueing what it finds"
    ),
    since_days: Optional[int] = typer.Option(
        None, "--since-days", help="only events touched in the last N days"
    ),
    queue: bool = typer.Option(True, "--queue/--no-queue", help="write to the review queue"),
) -> None:
    """Assert every non-identity factor actually multiplies rows.

    Catches the bug class that let HDFC Bank stay unadjusted with every other
    check green: a factor filed against an ISIN with no bars before its ex-date
    multiplies ZERO rows, so the price is never adjusted and nothing complains.

    Detection is CHAIN-AWARE, joining through silver.isin_link exactly as
    transform/adjusted.py does. Measured over the live panel: counting bars on
    the filed ISIN flags 271 (false positives), on canonical_isin alone 436
    (worse -- canonical is the LATEST in a chain), chain-aware 7 (correct).

    Exits 2 if an ORPHANED factor exists (wrong anchor -> re-anchor). pre_history
    findings (ex-date predates all coverage) never fail: they are unfixable, and
    a check that can never come back clean gets muted.
    """
    import datetime as _dt

    from .validate import factor_guard

    s = get_settings()
    bind_run(new_run_uid(), "assert-factors")
    since = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days)
        if since_days
        else None
    )

    findings = factor_guard.assert_factors_multiply_rows(s, since=since)
    if queue and findings:
        st = factor_guard.queue_orphans(findings, s)
        typer.echo(f"queued {st['queued']} new review row(s)")

    actionable = [f for f in findings if f.is_actionable]
    pre = [f for f in findings if not f.is_actionable]
    typer.echo(
        f"flagged {len(findings)}: {len(actionable)} ORPHANED (actionable), "
        f"{len(pre)} pre_history (unfixable, informational)"
    )
    for f in actionable:
        typer.echo(f"  ORPHANED    {f.describe()}")
    for f in pre[:10]:
        typer.echo(f"  pre_history {f.describe()}")
    if len(pre) > 10:
        typer.echo(f"  ... and {len(pre) - 10} more pre_history")

    if sweep:
        from .orchestrate.resolve import relabel_inferred_isin_switches

        n = relabel_inferred_isin_switches(s)
        typer.echo(
            f"relabelled {n} derived ISIN-switch row(s) to reason="
            "'inferred_isin_switch' so the three review categories are separable"
        )

    if actionable:
        typer.echo(
            "\nEXIT 2: an orphaned factor means a security's adjusted history is "
            "WRONG. Re-anchor it:  nse-eod review-queue resolve --review-id <ID> "
            "--action reanchor --to-isin <SURVIVING_ISIN> --confirm"
        )
        raise typer.Exit(2)


# -------------------------------------------------------- universe-status (A2)
@app.command("universe-status")
def universe_status_cmd(
    isin: Optional[str] = typer.Option(None, "--isin", help="look up one security"),
    show_blockers: bool = typer.Option(
        False, "--blockers", help="list the securities holding names out"
    ),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Verification status of the tradeable universe.

    Unverified names do not appear as candidates at all -- they are absent from
    gold.tradeable_universe rather than flagged inside it.
    """
    from .db import fetch_all, fetch_one
    from .transform.quality import isin_status

    if isin:
        row = isin_status(isin.strip().upper())
        if row is None:
            typer.echo(f"{isin}: not in silver.security_quality (no bars in the panel?)")
            raise typer.Exit(1)
        typer.echo(f"{row['isin']}  ({row['symbol'] or '?'})")
        typer.echo(f"  status            : {row['status']}")
        typer.echo(f"  open high-sev     : {row['open_high_sev']}")
        typer.echo(f"  orphaned factors  : {row['orphaned_factors']}")
        typer.echo(f"  unresolved breaks : {row['unresolved_breaks']}")
        typer.echo(f"  last bar          : {row['last_bar']}")
        typer.echo(f"  universe rows     : {row['universe_rows']:,}")
        typer.echo(f"  note              : {row['note'] or '-'}")
        if row["status"] != "verified":
            typer.echo("\n  NOT tradeable: absent from gold.tradeable_universe.")
        return

    for r in fetch_all("SELECT * FROM ops.v_universe_status ORDER BY status"):
        typer.echo(
            f"  {r['status']:<12} {r['securities']:>6} securities  "
            f"(high-sev {r['with_open_high_sev']}, orphaned {r['with_orphaned_factor']}, "
            f"breaks {r['with_unresolved_break']})"
        )

    u = fetch_one(
        """
        SELECT count(*) AS rows, count(DISTINCT isin) AS securities,
               max(trade_date) AS latest, max(built_at) AS built_at
          FROM gold.tradeable_universe
        """
    )
    typer.echo(
        f"\ngold.tradeable_universe: {u['rows'] or 0:,} rows | "
        f"{u['securities'] or 0:,} securities | latest {u['latest']} | "
        f"built {u['built_at']}"
    )
    if not u["rows"]:
        typer.echo("  EMPTY -- run `nse-eod run-daily` (it rebuilds after checks pass)")

    if show_blockers:
        typer.echo("\ntop unverified names by ADV (these gate real trades):")
        for r in fetch_all(
            """
            SELECT q.isin, q.symbol, q.status, q.note,
                   (SELECT max(adv_20) FROM gold.eod_adjusted g WHERE g.isin = q.isin)
                       AS adv
              FROM silver.security_quality q
             WHERE q.status <> 'verified'
             ORDER BY adv DESC NULLS LAST LIMIT %s
            """,
            (limit,),
        ):
            adv = f"{float(r['adv']):,.0f}" if r["adv"] else "-"
            typer.echo(
                f"  {(r['symbol'] or '?'):<12} {r['isin']:<14} {r['status']:<12} "
                f"adv={adv:>14}  {(r['note'] or '')[:60]}"
            )


# ------------------------------------------------------------ last-success (B)
@app.command("last-success")
def last_success_cmd(
    max_age_hours: float = typer.Option(
        30.0, "--max-age-hours", help="exit 2 if the last success is older than this"
    ),
    quiet: bool = typer.Option(False, "--quiet", help="print one line only"),
    heartbeat: bool = typer.Option(
        True,
        "--heartbeat/--no-heartbeat",
        help="publish HEARTBEAT_PATH for out-of-process consumers (the morning brief)",
    ),
) -> None:
    """Dead-man's switch. When did run-daily last complete?

    A silent no-run is the dangerous failure: signals fire on stale prices and
    nothing looks broken. `status IN (success, warning)` counts as success on
    purpose -- run-daily exits 1 for warnings, which is a completed run.

    Default 30h tolerance spans a normal weekday gap plus slack; a Monday morning
    check after a weekend legitimately sees ~65h, so use --max-age-hours there.
    """
    import datetime as _dt

    from .db import fetch_one
    from .orchestrate.heartbeat import publish

    r = fetch_one("SELECT * FROM ops.v_last_success")
    last = r["last_success_at"] if r else None

    # Publish BEFORE any Exit. Failing to publish must not change the freshness
    # verdict -- the caller's retry logic keys on that, not on whether a file was
    # written. A publish failure is self-reporting anyway: the brief then reads the
    # previous file and shows an old timestamp.
    if heartbeat:
        try:
            path = publish(r, max_age_hours=max_age_hours)
            if not quiet:
                typer.echo(f"  heartbeat         : {path}")
        except OSError as exc:
            typer.secho(f"WARN heartbeat not published: {exc}", fg="yellow", err=True)

    if last is None:
        typer.echo("EOD pipeline last ran: NEVER")
        raise typer.Exit(2)

    age_h = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds() / 3600.0
    typer.echo(
        f"EOD pipeline last ran: {last:%Y-%m-%d %H:%M:%S %Z} "
        f"({age_h:.1f}h ago, trade_date {r['last_success_trade_date']})"
    )
    if quiet:
        raise typer.Exit(0 if age_h <= max_age_hours else 2)

    typer.echo(f"  last attempt      : {r['last_attempt_at']} ({r['last_attempt_status']})")
    typer.echo(f"  universe built    : {r['universe_built_at']}")
    if age_h > max_age_hours:
        typer.echo(
            f"\nSTALE: older than {max_age_hours}h. The nightly run has not "
            "completed -- do not trade on these prices."
        )
        raise typer.Exit(2)


# -------------------------------------------------------------- review-queue
# -------------------------------------------------------------- review-queue
review_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and resolve corporate-action items awaiting a human decision.",
)
app.add_typer(review_app, name="review-queue")


@review_app.command("list")
def review_queue_list(
    severity: Optional[str] = typer.Option("high", "--severity", help="high|medium|low"),
    in_universe: bool = typer.Option(
        False, "--in-universe", help="only names that are actually tradeable"
    ),
    reason: Optional[str] = typer.Option(
        None,
        "--reason",
        help="needs_external_price | inferred_isin_switch | orphaned_factor "
        "| large_move_unexplained | pre_history | ...",
    ),
    limit: int = typer.Option(40, "--limit"),
) -> None:
    """Open items, UNIVERSE MEMBERS FIRST then by ADV.

    The ordering matters: most open high-severity items are illiquid SME
    ISIN-switches that will never be traded. Sorted alongside a mega-cap they sit
    on the critical path forever.
    """
    from .orchestrate.resolve import list_items

    rows = list_items(
        severity=severity, in_universe_only=in_universe, reason=reason, limit=limit
    )
    if not rows:
        typer.echo("no open items match")
        return

    n_univ = sum(1 for r in rows if r["in_universe"])
    typer.echo(f"{len(rows)} open item(s); {n_univ} in the tradeable universe")
    typer.echo("")
    typer.echo(
        f"{'UNIV':<5} {'ID':<7} {'SYMBOL':<12} {'EX-DATE':<11} {'REASON':<22} "
        f"{'QUALITY':<12} {'ADV(Rs)':>14}  PURPOSE"
    )
    for r in rows:
        adv = f"{float(r['adv_20']):,.0f}" if r["adv_20"] else "-"
        typer.echo(
            f"{'YES' if r['in_universe'] else '  .':<5} "
            f"#{r['review_id']:<6} {(r['symbol'] or '?'):<12} "
            f"{str(r['ex_date'] or '?'):<11} {r['reason']:<22} "
            f"{(r['quality_status'] or '-'):<12} {adv:>14}  "
            f"{(r['raw_purpose'] or '')[:56]}"
        )
    typer.echo("")
    typer.echo(
        "resolve with:  nse-eod review-queue resolve --review-id <ID> "
        "--action {factor|reanchor|accept|ignore} ..."
    )


@review_app.command("resolve")
def review_queue_resolve(
    review_id: int = typer.Option(..., "--review-id"),
    action: str = typer.Option(
        ..., "--action", help="factor | reanchor | accept | ignore"
    ),
    factor: Optional[str] = typer.Option(
        None, "--factor", help="human-supplied factor_price (action=factor)"
    ),
    to_isin: Optional[str] = typer.Option(
        None, "--to-isin", help="surviving ISIN holding the bars (action=reanchor)"
    ),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        help="provenance of the number, e.g. 'NSE demerger circular 2026/041'. "
        "MANDATORY for needs_external_price.",
    ),
    note: Optional[str] = typer.Option(None, "--note"),
    event_id: Optional[int] = typer.Option(
        None, "--event-id", help="disambiguate when a raw row fans out to several events"
    ),
    by: str = typer.Option("manual", "--by", help="who is resolving this"),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="REQUIRED for factor/reanchor: these change adjusted prices",
    ),
) -> None:
    """Record a human-supplied resolution, rebuild SCOPED, verify, then mark.

    Runs the scoped rebuild for just the affected ISINs, re-checks the flagged
    break with the audit's own thresholds, and only marks the item resolved and
    flips quality to verified IF the break actually clears. A number that does not
    clear the flag is still recorded, but the item stays open.
    """
    from .orchestrate.resolve import ResolveError, resolve

    bind_run(new_run_uid(), "review-queue-resolve")
    try:
        out = resolve(
            review_id=review_id,
            action=action,
            factor=factor,
            to_isin=to_isin,
            source=source,
            note=note,
            event_id=event_id,
            resolved_by=by,
            confirm=confirm,
        )
    except ResolveError as exc:
        typer.echo(f"REFUSED: {exc}")
        raise typer.Exit(2)

    typer.echo(f"review #{out.review_id}  action={out.action}  -> {out.marked}")
    typer.echo(f"  isins rebuilt : {', '.join(out.isins) or '-'}")
    typer.echo(f"  verification  : {out.detail}")
    typer.echo(f"  quality       : {out.status_before} -> {out.status_after}")
    if out.marked == "open":
        typer.echo("")
        typer.echo(
            "  the supplied number did NOT clear the flag, so the item stays open "
            "and the ISIN stays unverified. The attempt is recorded in "
            "resolution_note."
        )
        raise typer.Exit(1)


# ---------------------------------------------------------------------- doctor
@app.command()
def doctor() -> None:
    """Check config, DB connectivity, schema presence and data freshness."""
    from .db import fetch_all, fetch_one

    s = get_settings()
    typer.echo("--- config ---")
    safe_url = s.database_url
    if "@" in safe_url:
        head, tail = safe_url.split("@", 1)
        scheme = head.split("//", 1)[0]
        safe_url = f"{scheme}//***@{tail}"
    typer.echo(f"  database_url      : {safe_url}")
    typer.echo(f"  data_dir          : {s.data_dir}")
    typer.echo(f"  alert_webhook_url : {'set' if s.alert_webhook_url else 'NOT SET (DB-only)'}")
    typer.echo(f"  backfill_start    : {s.backfill_start}")

    typer.echo("--- database ---")
    try:
        v = fetch_one("SELECT version() AS v")
        typer.echo(f"  connected: {v['v'].split(',')[0]}")
    except Exception as exc:
        typer.echo(f"  CONNECTION FAILED: {exc}")
        raise typer.Exit(2)

    tables = fetch_all(
        """
        SELECT table_schema || '.' || table_name AS t
          FROM information_schema.tables
         WHERE table_schema IN ('bronze','silver','gold','ops')
         ORDER BY 1
        """
    )
    typer.echo(f"  objects: {len(tables)}")
    missing = {
        "bronze.eod_bhav_raw",
        "bronze.corp_action_raw",
        "silver.security_master",
        "silver.corp_action_event",
        "silver.corp_action_review_queue",
        "gold.eod_adjusted",
        "ops.pipeline_run",
    } - {t["t"] for t in tables}
    if missing:
        typer.echo(f"  MISSING: {sorted(missing)} -- run `nse-eod migrate`")
        raise typer.Exit(2)

    typer.echo("--- data ---")
    for label, q in [
        ("bronze bars", "SELECT count(*) n, max(trade_date) d FROM bronze.eod_bhav_raw"),
        ("gold bars", "SELECT count(*) n, max(trade_date) d FROM gold.eod_adjusted"),
        ("securities", "SELECT count(*) n, max(last_seen) d FROM silver.security_master"),
        ("ca events", "SELECT count(*) n, max(ex_date) d FROM silver.corp_action_event"),
    ]:
        r = fetch_one(q)
        typer.echo(f"  {label:<12}: {r['n']:>9} rows, latest {r['d']}")

    r = fetch_one(
        """
        SELECT count(*) FILTER (WHERE status='open') AS open,
               count(*) FILTER (WHERE status='open' AND severity='high') AS high
          FROM silver.corp_action_review_queue
        """
    )
    typer.echo(f"  review queue: {r['open']} open ({r['high']} high severity)")

    # Split the same way check_stale_factors does: a missing factor is only a
    # problem when an anchor bar was actually available.
    r = fetch_one(
        """
        SELECT count(*) FILTER (WHERE anchor_available)     AS real_gaps,
               count(*) FILTER (WHERE NOT anchor_available) AS outside_window
          FROM (
            SELECT EXISTS (
                     SELECT 1
                       FROM silver.isin_link l
                       JOIN bronze.eod_bhav_raw b ON b.isin = l.isin
                      WHERE l.canonical_isin = COALESCE(e.canonical_isin, e.isin)
                        AND b.trade_date < e.ex_date
                   ) AS anchor_available
              FROM silver.corp_action_event e
             WHERE e.superseded_at IS NULL
               AND e.ex_date <= CURRENT_DATE
               AND e.factor_state IN ('pending','needs_anchor','unresolved')
               AND e.type IN ('BONUS','SPLIT','FACE_VALUE_CHANGE','RIGHTS',
                              'SPECIAL_DIVIDEND','RETURN_OF_CAPITAL')
          ) t
        """
    )
    if r["real_gaps"]:
        tracked = fetch_one(
            """
            SELECT count(*) AS n FROM silver.corp_action_event e
             WHERE e.superseded_at IS NULL
               AND e.ex_date <= CURRENT_DATE
               AND e.factor_state IN ('pending','needs_anchor','unresolved')
               AND e.type IN ('BONUS','SPLIT','FACE_VALUE_CHANGE','RIGHTS',
                              'SPECIAL_DIVIDEND','RETURN_OF_CAPITAL')
               AND EXISTS (SELECT 1 FROM silver.corp_action_review_queue q
                            WHERE q.status='open' AND q.severity='high'
                              AND q.isin = e.isin AND q.ex_date = e.ex_date)
            """
        )["n"]
        untracked = r["real_gaps"] - tracked
        if untracked > 0:
            typer.echo(
                f"  PROBLEM: {untracked} past-dated price-adjusting event(s) have an "
                "available anchor bar, no factor, and NO review-queue entry (silent)"
            )
        if tracked:
            typer.echo(
                f"  warning: {tracked} event(s) lack a factor but are tracked in the "
                "high-severity review queue (awaiting a human decision)"
            )
    if r["outside_window"]:
        typer.echo(
            f"  info: {r['outside_window']} event(s) cannot be anchored -- no bar exists "
            "before their ex-date (history not backfilled that far)"
        )

    exit_code = 2 if r["real_gaps"] and untracked > 0 else 0
    typer.echo("ok" if exit_code == 0 else "issues found")
    if exit_code:
        raise typer.Exit(exit_code)


if __name__ == "__main__":
    app()
