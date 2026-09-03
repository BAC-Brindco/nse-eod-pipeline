"""Postgres access: pool, bulk upsert helpers, run ledger, watermarks."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
from typing import Any, Iterable, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Settings, get_settings
from .logging_setup import RunSummary, get_logger

log = get_logger(__name__)

_pool: ConnectionPool | None = None


# --------------------------------------------------------------------- pooling
def get_pool(settings: Settings | None = None) -> ConnectionPool:
    global _pool
    if _pool is None:
        s = settings or get_settings()
        _pool = ConnectionPool(
            conninfo=s.database_url,
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextlib.contextmanager
def connection(settings: Settings | None = None):
    """A transactional connection. Commits on clean exit, rolls back on error."""
    pool = get_pool(settings)
    with pool.connection() as conn:
        s = settings or get_settings()
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SET statement_timeout = {}").format(
                    sql.Literal(f"{s.db_statement_timeout_s}s")
                )
            )
        yield conn


def fetch_all(query: str, params: Sequence[Any] | None = None) -> list[dict]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def fetch_one(query: str, params: Sequence[Any] | None = None) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def execute(query: str, params: Sequence[Any] | None = None) -> int:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.rowcount


# ----------------------------------------------------------------- migrations
def apply_migrations(migrations_dir, settings: Settings | None = None) -> list[str]:
    """Apply every ``NNN_*.sql`` in order. Each file is idempotent (IF NOT EXISTS)."""
    from pathlib import Path

    applied: list[str] = []
    files = sorted(Path(migrations_dir).glob("[0-9][0-9][0-9]_*.sql"))
    s = settings or get_settings()
    # Migrations are DDL and must not run inside the pool's shared session state.
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        for f in files:
            log.info("migration_apply", file=f.name)
            with conn.cursor() as cur:
                cur.execute(f.read_text(encoding="utf-8"))
            applied.append(f.name)
    return applied


# -------------------------------------------------------------------- hashing
def row_hash(values: Iterable[Any]) -> str:
    """Stable md5 over a row's canonical payload.

    Used to detect NSE *restatements*: the natural key is unchanged but the
    numbers moved, which is the one case where re-running a day is not a no-op.
    """
    parts = []
    for v in values:
        if v is None:
            parts.append("")
        elif isinstance(v, float):
            parts.append(f"{v:.6f}")
        else:
            parts.append(str(v))
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------- bulk upserting
def upsert_rows(
    conn: psycopg.Connection,
    table: str,
    rows: list[dict],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    where_clause: str | None = None,
    page: int = 5000,
    conflict_target: str | None = None,
) -> int:
    """Batched ``INSERT ... ON CONFLICT DO UPDATE``.

    ``where_clause`` lets the caller make the UPDATE conditional, e.g. only when
    ``row_hash`` differs, so an unchanged re-run touches zero rows and leaves
    ``updated_at`` alone. That is what makes re-running a day a true no-op.

    ``conflict_target`` supplies a raw ON CONFLICT target for tables whose unique
    index is an EXPRESSION rather than a plain column list. Postgres matches the
    inference clause against the index definition literally, so an expression
    index needs the expression repeated verbatim.
    """
    if not rows:
        return 0

    cols = list(rows[0].keys())
    upd = [c for c in (update_cols if update_cols is not None else cols) if c not in conflict_cols]

    ident = lambda n: sql.Identifier(*n.split("."))  # noqa: E731
    col_ids = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    placeholders = sql.SQL(", ").join(sql.Placeholder(c) for c in cols)
    conflict = (
        sql.SQL(conflict_target)  # type: ignore[arg-type]
        if conflict_target
        else sql.SQL(", ").join(sql.Identifier(c) for c in conflict_cols)
    )

    if upd:
        set_clause = sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in upd
        )
        action = sql.SQL("DO UPDATE SET {s}").format(s=set_clause)
        if where_clause:
            action = action + sql.SQL(" WHERE ") + sql.SQL(where_clause)  # type: ignore[operator]
    else:
        action = sql.SQL("DO NOTHING")

    stmt = sql.SQL(
        "INSERT INTO {t} ({c}) VALUES ({p}) ON CONFLICT ({k}) {a}"
    ).format(t=ident(table), c=col_ids, p=placeholders, k=conflict, a=action)

    affected = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), page):
            chunk = rows[i : i + page]
            cur.executemany(stmt, chunk)
            affected += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return affected


def insert_ignore(
    conn: psycopg.Connection,
    table: str,
    rows: list[dict],
    conflict_cols: Sequence[str],
    page: int = 5000,
    conflict_target: str | None = None,
) -> int:
    """Append-only insert for bronze tables. Duplicates are silently skipped."""
    return upsert_rows(
        conn,
        table,
        rows,
        conflict_cols,
        update_cols=[],
        page=page,
        conflict_target=conflict_target,
    )


# The review queue is deduped by a UNIQUE index on an EXPRESSION, because
# derived items have a NULL ca_raw_id and NULL <> NULL would let them duplicate
# on every run (see migration 006).
REVIEW_QUEUE_CONFLICT = "COALESCE(ca_raw_id, -1), raw_purpose"


# ----------------------------------------------------------------- run ledger
def start_run(command: str, run_uid: str, target_date: dt.date | None = None) -> int:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.pipeline_run (run_uid, command, target_date, status)
            VALUES (%s, %s, %s, 'running')
            RETURNING run_id
            """,
            (run_uid, command, target_date),
        )
        return cur.fetchone()["run_id"]


def finish_run(
    run_id: int,
    status: str,
    exit_code: int,
    summary: RunSummary | None = None,
    error_text: str | None = None,
) -> None:
    counters = summary.counters() if summary else {}
    payload = json.dumps(summary.as_jsonb()) if summary else None
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.pipeline_run
               SET status = %s,
                   exit_code = %s,
                   finished_at = now(),
                   error_text = %s,
                   summary = %s::jsonb,
                   bhav_rows_ingested    = COALESCE(%s, bhav_rows_ingested),
                   bhav_rows_restated    = COALESCE(%s, bhav_rows_restated),
                   ca_rows_ingested      = COALESCE(%s, ca_rows_ingested),
                   ca_events_parsed      = COALESCE(%s, ca_events_parsed),
                   ca_flagged_for_review = COALESCE(%s, ca_flagged_for_review),
                   factors_computed      = COALESCE(%s, factors_computed),
                   gold_isins_rebuilt    = COALESCE(%s, gold_isins_rebuilt),
                   gold_rows_written     = COALESCE(%s, gold_rows_written),
                   alerts_raised         = COALESCE(%s, alerts_raised)
             WHERE run_id = %s
            """,
            (
                status,
                exit_code,
                error_text,
                payload,
                counters.get("bhav_rows_ingested"),
                counters.get("bhav_rows_restated"),
                counters.get("ca_rows_ingested"),
                counters.get("ca_events_parsed"),
                counters.get("ca_flagged_for_review"),
                counters.get("factors_computed"),
                counters.get("gold_isins_rebuilt"),
                counters.get("gold_rows_written"),
                counters.get("alerts_raised"),
                run_id,
            ),
        )


# ----------------------------------------------------------------- watermarks
def get_watermark(command: str, target_date: dt.date) -> dict | None:
    return fetch_one(
        "SELECT * FROM ops.run_watermark WHERE command = %s AND target_date = %s",
        (command, target_date),
    )


def set_watermark(
    command: str,
    target_date: dt.date,
    status: str,
    run_id: int | None = None,
    content_hash: str | None = None,
) -> None:
    execute(
        """
        INSERT INTO ops.run_watermark (command, target_date, status, run_id, content_hash, completed_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (command, target_date) DO UPDATE
           SET status = EXCLUDED.status,
               run_id = EXCLUDED.run_id,
               content_hash = EXCLUDED.content_hash,
               completed_at = now()
        """,
        (command, target_date, status, run_id, content_hash),
    )


def is_already_done(command: str, target_date: dt.date, content_hash: str | None = None) -> bool:
    """True when this (command, date) succeeded AND the source content is unchanged.

    A restated NSE file changes ``content_hash`` and correctly re-triggers work.
    """
    wm = get_watermark(command, target_date)
    if wm is None or wm["status"] != "success":
        return False
    if content_hash is None or wm["content_hash"] is None:
        return True
    return wm["content_hash"] == content_hash
