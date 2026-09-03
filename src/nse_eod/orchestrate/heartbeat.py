"""Publish pipeline freshness to a file that OTHER PROCESSES can read.

WHY A FILE AND NOT A DB QUERY
-----------------------------
The consumer is the morning brief, which lives in a different repo, a different
venv and a different scheduled job. Handing it a DSN would mean copying database
credentials into a second codebase and giving the brief a hard dependency on
Postgres being up. That dependency is backwards: the moment we most need the
brief to shout "the EOD pipeline has not run" is precisely when the pipeline's
environment is broken.

So the pipeline pushes a tiny JSON stamp and the brief reads it. The brief needs
no credentials, no driver and no network.

WHY THE CONTENT IS DB-DERIVED AND THE MTIME IS IGNORED
------------------------------------------------------
`last_success_at` comes from ops.v_last_success, i.e. from the run rows
themselves. Running `last-success` by hand at 3pm rewrites the file but does NOT
move the timestamp forward. So a fresh file with a stale payload still reads as
stale, and consumers must compute age from `last_success_at`. If the age were
taken from the file's mtime, any invocation of the reporting command would look
like a successful run -- exactly the failure this switch exists to catch.

WHY IT IS WRITTEN ATOMICALLY
----------------------------
The brief runs at ~07:30 IST and the pipeline at 19:00, but a manual re-run or a
Task Scheduler retry can overlap anything. os.replace is atomic on Windows for a
same-volume rename, so a reader never sees a truncated file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Mapping

from ..config import Settings, get_settings

SCHEMA_VERSION = 1


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return str(v)


def build(row: Mapping[str, Any] | None, max_age_hours: float = 30.0) -> dict[str, Any]:
    """Shape the payload. Pure, so the test suite can assert on it without IO."""
    last = row.get("last_success_at") if row else None
    now = dt.datetime.now(dt.timezone.utc)

    age_h: float | None = None
    if isinstance(last, dt.datetime):
        # ops.v_last_success returns timestamptz, so `last` is aware. Guard anyway:
        # a naive value here would raise on subtraction and take down the switch.
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        age_h = (now - last).total_seconds() / 3600.0

    return {
        "schema_version": SCHEMA_VERSION,
        "component": "nse_eod",
        # None => the pipeline has NEVER completed. Consumers must treat this as
        # worse than stale, not as missing data.
        "last_success_at": _iso(last),
        "last_success_trade_date": _iso(row.get("last_success_trade_date")) if row else None,
        "last_attempt_at": _iso(row.get("last_attempt_at")) if row else None,
        "last_attempt_status": (row.get("last_attempt_status") if row else None),
        "universe_built_at": _iso(row.get("universe_built_at")) if row else None,
        "age_hours": None if age_h is None else round(age_h, 2),
        "max_age_hours": max_age_hours,
        "stale": True if age_h is None else age_h > max_age_hours,
        "published_at": now.isoformat(),
    }


def publish(
    row: Mapping[str, Any] | None,
    max_age_hours: float = 30.0,
    settings: Settings | None = None,
) -> Path:
    s = settings or get_settings()
    path = Path(s.heartbeat_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(build(row, max_age_hours), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(path: str | Path) -> dict[str, Any] | None:
    """Consumer-side helper. Returns None when the stamp is absent or unreadable.

    Kept here so the format has exactly one owner, but the morning brief
    deliberately does NOT import it -- see brief_freshness in that repo. A cross-
    repo import would put this package on the brief's PYTHONPATH and make a broken
    pipeline install able to break the brief.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
