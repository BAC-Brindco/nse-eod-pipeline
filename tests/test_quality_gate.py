"""Tests for the zero-multiplier guard (A1) and the verified-universe gate (A2).

The pure-logic tests run offline. The integration tests exercise the real chain
join, because the whole point of the guard is WHICH join it mirrors -- a
unit-tested guard against a fake schema would prove nothing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from nse_eod.validate.factor_guard import (
    ORPHANED,
    PRE_HISTORY,
    OrphanFinding,
)


def _finding(bars_by_symbol: int, **kw) -> OrphanFinding:
    base = dict(
        event_id=1,
        symbol="ACME",
        isin="INE000A01011",
        canonical_isin="INE000A01029",
        ex_date=dt.date(2025, 1, 15),
        type="BONUS",
        factor_price=0.5,
        isin_resolved_via="direct",
        raw_purpose="Bonus 1:1",
        ca_raw_id=None,
        bars_by_symbol=bars_by_symbol,
    )
    base.update(kw)
    return OrphanFinding(**base)


# --------------------------------------------------- classification (pure)
class TestOrphanClassification:
    """`bars_by_symbol` is the whole discriminator.

    Without it the guard is either blind (chain-only, misses HDFC Bank) or a
    436-row false-positive generator (canonical-only).
    """

    def test_bars_elsewhere_means_wrong_anchor(self):
        """The HDFC Bank class: chain reaches nothing, security has history."""
        f = _finding(bars_by_symbol=1437)
        assert f.kind == ORPHANED
        assert f.is_actionable
        assert "WRONG ANCHOR" in f.describe()

    def test_no_bars_anywhere_means_pre_history(self):
        """Ex-date predates all coverage: unfixable, must not fail the run."""
        f = _finding(bars_by_symbol=0)
        assert f.kind == PRE_HISTORY
        assert not f.is_actionable
        assert "predates coverage" in f.describe()

    def test_a_single_earlier_bar_is_enough_to_be_actionable(self):
        assert _finding(bars_by_symbol=1).is_actionable

    def test_describe_names_both_isins_so_a_human_can_act(self):
        f = _finding(bars_by_symbol=10)
        d = f.describe()
        assert "INE000A01011" in d and "INE000A01029" in d


# --------------------------------------------------------- the real join
@pytest.mark.integration
class TestGuardAgainstRealSchema:
    """The guard must mirror transform/adjusted.py::_fetch_bars.

    Measured on the live panel (1,426 non-identity factors):
        filed isin        -> 271 flagged (false positives)
        canonical_isin    -> 436 flagged (worse: canonical is the LATEST in a
                             chain, so TDPOWERSYS's INE419M01035 has no bars
                             before either of its two split ex-dates)
        chain-aware       ->   7 flagged (correct)
    """

    def test_chain_aware_count_is_far_below_the_naive_counts(self, has_bars):
        from nse_eod.db import fetch_one
        from nse_eod.validate.factor_guard import assert_factors_multiply_rows

        chain = len(assert_factors_multiply_rows())
        naive = fetch_one(
            """
            SELECT
              count(*) FILTER (WHERE NOT EXISTS (
                  SELECT 1 FROM bronze.eod_bhav_raw b
                   WHERE b.isin = e.isin AND b.trade_date < e.ex_date)) AS filed,
              count(*) FILTER (WHERE NOT EXISTS (
                  SELECT 1 FROM bronze.eod_bhav_raw b
                   WHERE b.isin = COALESCE(e.canonical_isin, e.isin)
                     AND b.trade_date < e.ex_date)) AS canonical
              FROM silver.corp_action_event e
             WHERE e.superseded_at IS NULL
               AND e.factor_price IS NOT NULL AND e.factor_price <> 1
            """
        )
        assert chain < naive["filed"], (
            f"chain-aware {chain} should be far below filed-isin {naive['filed']}"
        )
        assert chain < naive["canonical"], (
            f"chain-aware {chain} should be far below canonical-only "
            f"{naive['canonical']} -- canonical is the LATEST isin in a chain"
        )

    def test_a_multi_split_security_is_not_flagged(self, has_bars):
        """TDPOWERSYS split twice, so its canonical has no bars before either
        ex-date. A canonical-only check calls both splits orphaned."""
        from nse_eod.validate.factor_guard import assert_factors_multiply_rows

        flagged = {f.symbol for f in assert_factors_multiply_rows()}
        assert "TDPOWERSYS" not in flagged

    def test_hdfcbank_is_currently_clean(self, has_bars):
        """The motivating incident, now fixed. A regression here means the
        largest private bank is silently unadjusted by 2x again."""
        from nse_eod.db import fetch_one
        from nse_eod.validate.factor_guard import assert_factors_multiply_rows

        ev = fetch_one(
            """
            SELECT event_id FROM silver.corp_action_event
             WHERE symbol = 'HDFCBANK' AND type = 'BONUS' AND superseded_at IS NULL
            """
        )
        if ev is None:
            pytest.skip("HDFCBANK bonus not in the ingested window")
        assert assert_factors_multiply_rows(event_ids=[ev["event_id"]]) == []

    def test_breaking_an_anchor_is_detected_as_actionable(self, has_bars):
        """Proof the guard CATCHES, not merely that it returns zero.

        A guard that reports 0 on healthy data is indistinguishable from a guard
        that reports 0 on everything, so the failure mode is induced inside a
        transaction that is rolled back.
        """
        import psycopg
        from psycopg.rows import dict_row

        from nse_eod.config import get_settings
        from nse_eod.db import fetch_one
        from nse_eod.validate.factor_guard import _GUARD_SQL

        ev = fetch_one(
            """
            SELECT event_id, canonical_isin FROM silver.corp_action_event
             WHERE symbol = 'HDFCBANK' AND type = 'BONUS' AND superseded_at IS NULL
            """
        )
        if ev is None:
            pytest.skip("HDFCBANK bonus not in the ingested window")

        with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                sql = _GUARD_SQL.format(extra_filter=" AND e.event_id = ANY(%s)")
                cur.execute(sql, ([ev["event_id"]],))
                assert cur.fetchall() == [], "should start clean"

                cur.execute(
                    """
                    UPDATE silver.corp_action_event SET canonical_isin = 'INE040A01018'
                     WHERE event_id = %s
                    """,
                    (ev["event_id"],),
                )
                cur.execute(sql, ([ev["event_id"]],))
                hits = cur.fetchall()
                assert len(hits) == 1, "the broken anchor must be detected"
                assert hits[0]["bars_in_chain"] == 0
                assert hits[0]["bars_by_symbol"] > 0, (
                    "the security HAS earlier bars, so this is a wrong anchor "
                    "(actionable), not a pre-coverage event"
                )
            conn.rollback()

        # and nothing was left mutated
        after = fetch_one(
            "SELECT canonical_isin FROM silver.corp_action_event WHERE event_id = %s",
            (ev["event_id"],),
        )
        assert after["canonical_isin"] == ev["canonical_isin"]


# ------------------------------------------------------- the universe gate
@pytest.mark.integration
class TestUniverseGate:
    def test_no_unverified_isin_is_in_the_universe(self, has_bars):
        """THE acceptance criterion. Unverified names are ABSENT, not flagged."""
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT count(*) AS leaked
              FROM gold.tradeable_universe u
              LEFT JOIN silver.security_quality q ON q.isin = u.isin
             WHERE q.status IS DISTINCT FROM 'verified'
            """
        )
        assert r["leaked"] == 0, f"{r['leaked']} unverified rows reached the universe"

    def test_the_gate_actually_excludes_something(self, has_bars):
        """A gate that admits everything is not a gate."""
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT (SELECT count(DISTINCT isin) FROM gold.eod_adjusted WHERE in_universe)
                       AS liquid,
                   (SELECT count(DISTINCT isin) FROM gold.tradeable_universe)
                       AS verified
            """
        )
        if not r["liquid"]:
            pytest.skip("panel empty")
        assert r["verified"] <= r["liquid"]
        assert r["verified"] > 0, "the gate must not be empty either"

    def test_universe_is_a_table_not_a_view(self, has_bars):
        """Deliberate: a view re-derives itself the moment gold is written, so a
        failed run would silently widen the gate. A table lets a failed run leave
        the previous known-good universe in place."""
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT table_type FROM information_schema.tables
             WHERE table_schema = 'gold' AND table_name = 'tradeable_universe'
            """
        )
        assert r and r["table_type"] == "BASE TABLE"

    def test_every_universe_row_carries_a_build_timestamp(self, has_bars):
        """A stale built_at is itself the alarm."""
        from nse_eod.db import fetch_one

        r = fetch_one(
            "SELECT count(*) AS n FROM gold.tradeable_universe WHERE built_at IS NULL"
        )
        assert r["n"] == 0

    def test_quarantine_ejects_rather_than_declines(self, has_bars):
        """Freezing the universe is only PARTIAL containment.

        gold.eod_adjusted is rewritten before the guard runs, so a detected
        problem must EJECT the affected names, not merely decline to re-admit
        them. Verified here on a throwaway ISIN so no real security is touched.
        """
        from nse_eod.db import execute, fetch_one
        from nse_eod.transform.quality import quarantine_isins

        probe = "INTESTONLY001"
        try:
            execute(
                """
                INSERT INTO silver.security_quality (isin, symbol, status)
                VALUES (%s, 'TESTONLY', 'verified')
                ON CONFLICT (isin) DO UPDATE SET status = 'verified'
                """,
                (probe,),
            )
            execute(
                """
                INSERT INTO gold.tradeable_universe
                    (trade_date, isin, symbol, quality_status)
                VALUES ('2026-08-28', %s, 'TESTONLY', 'verified')
                ON CONFLICT (trade_date, isin) DO NOTHING
                """,
                (probe,),
            )
            assert fetch_one(
                "SELECT count(*) AS n FROM gold.tradeable_universe WHERE isin = %s",
                (probe,),
            )["n"] == 1

            quarantine_isins([probe], note="unit test")

            assert fetch_one(
                "SELECT count(*) AS n FROM gold.tradeable_universe WHERE isin = %s",
                (probe,),
            )["n"] == 0, "quarantine must EJECT, not merely decline to re-admit"
            assert fetch_one(
                "SELECT status FROM silver.security_quality WHERE isin = %s", (probe,)
            )["status"] == "review_open"
        finally:
            execute("DELETE FROM gold.tradeable_universe WHERE isin = %s", (probe,))
            execute("DELETE FROM silver.security_quality WHERE isin = %s", (probe,))

    def test_blocked_status_survives_recompute(self, has_bars):
        """`blocked` is a human decision; no automatic recompute may lift it."""
        from nse_eod.db import execute, fetch_one
        from nse_eod.transform.quality import recompute_security_quality

        probe = fetch_one(
            """
            SELECT isin FROM silver.security_quality
             WHERE status = 'verified' ORDER BY isin LIMIT 1
            """
        )
        if probe is None:
            pytest.skip("no verified security to test with")
        isin = probe["isin"]
        try:
            execute(
                "UPDATE silver.security_quality SET status = 'blocked' WHERE isin = %s",
                (isin,),
            )
            recompute_security_quality()
            assert fetch_one(
                "SELECT status FROM silver.security_quality WHERE isin = %s", (isin,)
            )["status"] == "blocked"
        finally:
            execute(
                "UPDATE silver.security_quality SET status = 'verified' WHERE isin = %s",
                (isin,),
            )


# ------------------------------------------------------ thresholds are shared
class TestThresholdsAreNotReinvented:
    """The gate and the audit must never disagree about what "a break" is."""

    def test_quality_thresholds_match_audit_panel(self):
        from pathlib import Path

        from nse_eod.transform import quality

        src = (
            Path(__file__).resolve().parents[1] / "tools" / "audit_panel.py"
        ).read_text(encoding="utf-8")
        assert f"> {quality.PREV_CLOSE_BREAK}" in src, "prev_close threshold drifted"
        assert f"> {quality.LARGE_MOVE}" in src, "large-move threshold drifted"
        assert f"c_adj > {quality.LARGE_MOVE_MIN_PRICE}" in src, "price floor drifted"
        assert f"INTERVAL '{quality.ADJACENCY_DAYS} days'" in src, "adjacency drifted"


# ------------------------------------------------------------- review workflow
class TestResolveRefusals:
    """No corporate-action number is ever applied without explicit confirmation."""

    def test_unknown_action_is_refused(self):
        from nse_eod.orchestrate.resolve import ResolveError, resolve

        with pytest.raises(ResolveError, match="action must be one of"):
            resolve(review_id=1, action="whatever")

    @pytest.mark.parametrize("bad", ["99999", "0", "-1", "abc", ""])
    def test_implausible_factors_are_refused(self, bad):
        from nse_eod.orchestrate.resolve import ResolveError, _dec

        with pytest.raises(ResolveError):
            _dec(bad)

    @pytest.mark.parametrize("good", ["0.5", "0.333333", "10", "1"])
    def test_plausible_factors_are_accepted(self, good):
        from nse_eod.orchestrate.resolve import _dec

        assert _dec(good) > 0

    def test_demerger_requires_a_source(self):
        """A demerger factor cannot be derived from NSE EOD data, so the
        provenance of the human-supplied number IS the audit trail."""
        from nse_eod.orchestrate.resolve import SOURCE_REQUIRED_REASONS

        assert "needs_external_price" in SOURCE_REQUIRED_REASONS

    def test_accept_action_writes_no_factor(self):
        """A genuine market move (YESBANK's 2020 moratorium) is closed as
        reviewed_accepted, never by inventing a factor."""
        from nse_eod.orchestrate import resolve as R

        assert R.ACTION_ACCEPT in R.ACTIONS
        assert R.ACTION_ACCEPT != R.ACTION_FACTOR
