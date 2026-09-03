"""The dead-man's switch: the stamp the morning brief reads.

The property that matters is NOT "does it write a file" -- it is that the file
cannot be made to LIE. Specifically: running the reporting command must never make
a pipeline that has not run look like one that has. The tests below pin that,
because it is the only way this switch can fail silently and it is exactly the
mistake a well-meaning refactor would introduce (stamping now() is the obvious
thing to do and is wrong).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from nse_eod.orchestrate import heartbeat


def _row(last: dt.datetime | None, trade_date: dt.date | None = None) -> dict:
    return {
        "last_success_at": last,
        "last_success_trade_date": trade_date or dt.date(2026, 9, 1),
        "last_attempt_at": last,
        "last_attempt_status": "success",
        "universe_built_at": last,
    }


def test_age_derives_from_the_run_not_from_now():
    """The payload's age must come from the run row, never from wall-clock.

    If age were computed from the moment of publication, any invocation of
    `last-success` would reset the switch -- the reporting tool would forge the
    signal it reports on.
    """
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=72)
    p = heartbeat.build(_row(old), max_age_hours=30.0)
    assert p["age_hours"] == pytest.approx(72.0, abs=0.1)
    assert p["stale"] is True
    # published_at moves; last_success_at does not.
    assert p["published_at"] > p["last_success_at"]


def test_never_run_is_stale_not_merely_absent():
    p = heartbeat.build(_row(None))
    assert p["last_success_at"] is None
    assert p["stale"] is True, "a pipeline that never ran must not read as fresh"
    assert p["age_hours"] is None


def test_no_row_at_all_is_stale():
    """ops.v_last_success returning nothing must not produce an optimistic stamp."""
    p = heartbeat.build(None)
    assert p["stale"] is True
    assert p["last_success_at"] is None


def test_fresh_run_inside_tolerance_is_not_stale():
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12)
    p = heartbeat.build(_row(recent), max_age_hours=30.0)
    assert p["stale"] is False


def test_naive_timestamp_does_not_crash_the_switch():
    """A naive datetime would raise on subtraction and take the switch down.

    ops.v_last_success returns timestamptz so this should not occur, but the
    switch must degrade rather than explode if the column type ever changes.
    """
    naive = dt.datetime.now() - dt.timedelta(hours=5)
    p = heartbeat.build(_row(naive))
    assert p["age_hours"] is not None


def test_publish_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    from nse_eod.config import get_settings

    s = get_settings()
    target = tmp_path / "nested" / "heartbeat.json"
    monkeypatch.setattr(s, "heartbeat_path", target, raising=False)

    out = heartbeat.publish(_row(dt.datetime.now(dt.timezone.utc)), settings=s)
    assert out == target
    assert json.loads(target.read_text(encoding="utf-8"))["component"] == "nse_eod"
    assert not list(tmp_path.rglob("*.tmp")), "temp file left behind"


def test_publish_overwrites_in_place(tmp_path, monkeypatch):
    from nse_eod.config import get_settings

    s = get_settings()
    target = tmp_path / "heartbeat.json"
    monkeypatch.setattr(s, "heartbeat_path", target, raising=False)

    t1 = dt.datetime(2026, 8, 30, 19, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 9, 1, 19, 0, tzinfo=dt.timezone.utc)
    heartbeat.publish(_row(t1), settings=s)
    heartbeat.publish(_row(t2), settings=s)
    assert json.loads(target.read_text(encoding="utf-8"))["last_success_at"].startswith(
        "2026-09-01"
    )


def test_read_fails_soft(tmp_path):
    assert heartbeat.read(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert heartbeat.read(bad) is None


def test_trade_date_is_carried_because_consumers_judge_coverage_by_it():
    """The brief compares this against the session it describes, so it must survive."""
    p = heartbeat.build(_row(dt.datetime.now(dt.timezone.utc), dt.date(2026, 8, 28)))
    assert p["last_success_trade_date"] == "2026-08-28"
