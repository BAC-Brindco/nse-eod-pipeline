"""Shared fixtures. Unit tests never touch the network or the DB."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _db_available() -> bool:
    """True when a Postgres with the pipeline schema is reachable."""
    try:
        from nse_eod.db import fetch_one

        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def db():
    """Session-scoped guard for integration tests."""
    if os.environ.get("NSE_EOD_SKIP_DB"):
        pytest.skip("NSE_EOD_SKIP_DB set")
    if not _db_available():
        pytest.skip("no Postgres reachable at DATABASE_URL")
    from nse_eod.db import fetch_one

    n = fetch_one(
        """
        SELECT count(*) AS n
          FROM information_schema.tables
         WHERE table_schema IN ('bronze','silver','gold','ops')
        """
    )["n"]
    if n < 10:
        pytest.skip("schema not migrated; run `nse-eod migrate`")
    return True


@pytest.fixture(scope="session")
def has_bars(db):
    """Skip unless bronze actually holds bars to test against."""
    from nse_eod.db import fetch_one

    r = fetch_one("SELECT count(*) AS n, max(trade_date) AS d FROM bronze.eod_bhav_raw")
    if not r or r["n"] == 0:
        pytest.skip("bronze is empty; run `nse-eod backfill` first")
    return r["d"]


@pytest.fixture(scope="session")
def fresh_panel(has_bars):
    """Guarantee gold reflects the CURRENT code before asserting invariants.

    Without this, an invariant test can fail on rows materialized by an earlier
    version of the universe/factor logic -- a stale-state false positive rather
    than a real regression.
    """
    from nse_eod.db import execute
    from nse_eod.transform import isin_link
    from nse_eod.transform.adjusted import materialize
    from nse_eod.transform.anchors import compute_pending_factors

    isin_link.refresh()
    compute_pending_factors()
    execute("TRUNCATE gold.eod_adjusted")
    materialize(None)
    return has_bars


@pytest.fixture
def fake_session():
    """An NSESession stand-in that serves the on-disk fixtures.

    Keeps ingest tests fully offline: unit tests must never hit live NSE.
    """

    class FakeSession:
        def __init__(self):
            self.calls: list[str] = []
            self.archived: list[str] = []

        def get_bytes(self, url: str, referer: str = "", params=None) -> bytes:
            self.calls.append(url)
            if "BhavCopy_NSE_CM" in url:
                return (FIXTURES / "udiff_20260828.csv").read_bytes()
            if "sec_bhavdata_full" in url:
                return (FIXTURES / "delivery_28082026.csv").read_bytes()
            if "sec_list" in url:
                return (FIXTURES / "pricebands_28082026.csv").read_bytes()
            if "EQUITY_L" in url:
                return (FIXTURES / "equity_l.csv").read_bytes()
            from nse_eod.sources.http import NSENotFound

            raise NSENotFound(f"no fixture for {url}")

        def get_json(self, url: str, referer: str = "", params=None):
            return []

        def warm(self, force: bool = False) -> None:
            pass

        def reset(self) -> None:
            pass

        def archive(self, content: bytes, name: str):
            self.archived.append(name)
            return None

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    return FakeSession()


@pytest.fixture
def fixture_date() -> dt.date:
    return dt.date(2026, 8, 28)
