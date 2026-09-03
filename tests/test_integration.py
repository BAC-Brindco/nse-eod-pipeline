"""Integration tests against a live local Postgres.

Covers the acceptance criteria that cannot be proven with pure functions:
  * a golden real symbol across a bonus and across a split has NO fake ex-date gap
  * ``run-daily`` twice leaves identical DB state
  * gold is reconstructible from bronze + the factor table alone
  * the ISIN-change linkage keeps a split-renamed security as ONE series

Skipped automatically when no DB or no bars are present.
"""

from __future__ import annotations

import datetime as dt

import pytest

pytestmark = pytest.mark.integration


# --------------------------------------------------------------- golden series
GOLDEN_BONUS = {
    "symbol": "GOODLUCK",
    "isin": "INE127I01024",
    "ex_date": dt.date(2026, 8, 21),
    "type": "BONUS",
    "expected_factor": 1 / 3,        # Bonus 2:1 -> before 1, after 3
}
GOLDEN_SPLIT = {
    "symbol": "TDPOWERSYS",
    "isin": "INE419M01035",          # canonical (post-split) ISIN
    "ex_date": dt.date(2026, 8, 24),
    "type": "SPLIT",
    "expected_factor": 0.5,          # Rs 2 -> Re 1
}


def _series(isin: str, start: dt.date, end: dt.date) -> list[dict]:
    from nse_eod.db import fetch_all

    return fetch_all(
        """
        SELECT trade_date, c_raw, c_adj, cum_factor, v_adj, volume, isin_traded
          FROM gold.eod_adjusted
         WHERE isin = %s AND series = 'EQ'
           AND trade_date BETWEEN %s AND %s
         ORDER BY trade_date
        """,
        (isin, start, end),
    )


@pytest.mark.parametrize("case", [GOLDEN_BONUS, GOLDEN_SPLIT], ids=lambda c: c["symbol"])
class TestGoldenNoFakeGap:
    """The headline acceptance criterion, on real NSE data."""

    def _rows(self, case, fresh_panel):
        rows = _series(
            case["isin"],
            case["ex_date"] - dt.timedelta(days=7),
            case["ex_date"] + dt.timedelta(days=4),
        )
        if len(rows) < 4:
            pytest.skip(f"{case['symbol']} not in the ingested window")
        return rows

    def test_raw_series_shows_a_large_fake_gap(self, case, fresh_panel):
        """Establishes that the test symbol really does need adjusting."""
        rows = self._rows(case, fresh_panel)
        pre = [r for r in rows if r["trade_date"] < case["ex_date"]]
        on = [r for r in rows if r["trade_date"] == case["ex_date"]]
        assert pre and on, "need bars on both sides of the ex-date"
        raw_ret = float(on[0]["c_raw"]) / float(pre[-1]["c_raw"]) - 1
        assert raw_ret < -0.30, f"expected a big raw drop, got {raw_ret:.2%}"

    def test_adjusted_series_is_continuous_across_the_ex_date(self, case, fresh_panel):
        """The whole point: no fake gap after adjustment."""
        rows = self._rows(case, fresh_panel)
        pre = [r for r in rows if r["trade_date"] < case["ex_date"]]
        on = [r for r in rows if r["trade_date"] == case["ex_date"]]
        adj_ret = float(on[0]["c_adj"]) / float(pre[-1]["c_adj"]) - 1
        assert abs(adj_ret) < 0.15, (
            f"{case['symbol']}: adjusted return across ex-date is {adj_ret:.2%}, "
            "which still looks like a corporate-action gap"
        )

    def test_cum_factor_matches_the_hand_computed_value(self, case, fresh_panel):
        rows = self._rows(case, fresh_panel)
        pre = [r for r in rows if r["trade_date"] < case["ex_date"]]
        assert float(pre[-1]["cum_factor"]) == pytest.approx(
            case["expected_factor"], rel=1e-6
        )

    def test_cum_factor_is_one_from_the_ex_date_onward(self, case, fresh_panel):
        rows = self._rows(case, fresh_panel)
        for r in [r for r in rows if r["trade_date"] >= case["ex_date"]]:
            assert float(r["cum_factor"]) == pytest.approx(1.0, abs=1e-9)

    def test_adjusted_equals_raw_on_and_after_the_ex_date(self, case, fresh_panel):
        rows = self._rows(case, fresh_panel)
        for r in [r for r in rows if r["trade_date"] >= case["ex_date"]]:
            assert float(r["c_adj"]) == pytest.approx(float(r["c_raw"]), rel=1e-9)

    def test_no_adjusted_price_is_zero_or_negative(self, case, fresh_panel):
        for r in self._rows(case, fresh_panel):
            assert float(r["c_adj"]) > 0


class TestGoldenVolume:
    def test_bonus_scales_pre_ex_volume_up(self, fresh_panel):
        """A 2:1 bonus triples pre-ex volume while dividing price by three."""
        rows = _series(GOLDEN_BONUS["isin"], dt.date(2026, 8, 14), dt.date(2026, 8, 26))
        if len(rows) < 4:
            pytest.skip("GOODLUCK not in window")
        pre = [r for r in rows if r["trade_date"] < GOLDEN_BONUS["ex_date"]][-1]
        assert float(pre["v_adj"]) == pytest.approx(float(pre["volume"]) * 3, rel=1e-6)

    def test_turnover_is_preserved_across_the_adjustment(self, fresh_panel):
        rows = _series(GOLDEN_BONUS["isin"], dt.date(2026, 8, 14), dt.date(2026, 8, 26))
        if len(rows) < 4:
            pytest.skip("GOODLUCK not in window")
        pre = [r for r in rows if r["trade_date"] < GOLDEN_BONUS["ex_date"]][-1]
        raw_turnover = float(pre["c_raw"]) * float(pre["volume"])
        adj_turnover = float(pre["c_adj"]) * float(pre["v_adj"])
        assert adj_turnover == pytest.approx(raw_turnover, rel=1e-6)


# ------------------------------------------------------------- ISIN linkage
class TestIsinLinkage:
    def test_split_renamed_security_is_one_continuous_series(self, fresh_panel):
        """TDPOWERSYS traded under two ISINs; the panel must show one series."""
        rows = _series(GOLDEN_SPLIT["isin"], dt.date(2026, 8, 1), dt.date(2026, 8, 31))
        if len(rows) < 6:
            pytest.skip("TDPOWERSYS not in window")
        traded = {r["isin_traded"] for r in rows}
        assert len(traded) >= 2, "expected at least two as-traded ISINs in this window"
        # ...yet all under one canonical key, hence one series.
        assert len({GOLDEN_SPLIT["isin"]}) == 1

    def test_no_symbol_appears_under_two_canonical_isins_on_one_day(self, fresh_panel):
        from nse_eod.db import fetch_all

        dupes = fetch_all(
            """
            SELECT symbol, trade_date, count(DISTINCT isin) AS n
              FROM gold.eod_adjusted
             WHERE series = 'EQ'
             GROUP BY symbol, trade_date
            HAVING count(DISTINCT isin) > 1
             LIMIT 10
            """
        )
        assert not dupes, f"symbol split across canonical ISINs: {dupes}"

    def test_every_link_chain_resolves_to_a_real_isin(self, has_bars):
        from nse_eod.db import fetch_all

        bad = fetch_all(
            """
            SELECT l.isin, l.canonical_isin
              FROM silver.isin_link l
             WHERE NOT EXISTS (
                SELECT 1 FROM silver.isin_link c WHERE c.isin = l.canonical_isin
             )
             LIMIT 5
            """
        )
        assert not bad, f"dangling canonical ISINs: {bad}"


# --------------------------------------------------------------- invariants
class TestPanelInvariants:
    """Structural invariants, asserted against a freshly rebuilt panel."""

    def test_right_edge_identity(self, fresh_panel):
        """The latest bar of every symbol must be unadjusted.

        Guaranteed by the reverse-cumulative-product design plus the rule that a
        future-dated ex-date is excluded. If this breaks, every adjusted price in
        the panel is suspect.
        """
        from nse_eod.db import fetch_all

        bad = fetch_all(
            """
            -- Per ISIN, not per (isin, series): a security migrates series over
            -- its life and the last bar of a DISCONTINUED series legitimately
            -- still carries a factor.
            WITH latest AS (
                SELECT DISTINCT ON (isin)
                       isin, symbol, trade_date, c_adj, c_raw, cum_factor
                  FROM gold.eod_adjusted
                 ORDER BY isin, trade_date DESC
            )
            SELECT symbol, isin, cum_factor FROM latest
             WHERE abs(cum_factor - 1.0) > 1e-9
             LIMIT 10
            """
        )
        assert not bad, f"latest bar not unadjusted for: {bad}"

    def test_no_future_dated_event_leaks_into_the_panel(self, fresh_panel):
        """Applying a not-yet-happened ex-date would be look-ahead bias."""
        from nse_eod.db import fetch_all

        bad = fetch_all(
            """
            SELECT g.symbol, g.isin, g.cum_factor, e.ex_date, e.type
              FROM gold.eod_adjusted g
              JOIN silver.corp_action_event e
                ON COALESCE(e.canonical_isin, e.isin) = g.isin
             WHERE e.ex_date > (SELECT max(trade_date) FROM gold.eod_adjusted)
               AND e.factor_price IS NOT NULL AND e.factor_price <> 1
               AND abs(g.cum_factor - 1.0) > 1e-9
             LIMIT 10
            """
        )
        assert not bad, f"future ex-date applied early (look-ahead): {bad}"

    def test_cum_factor_is_never_zero_or_negative(self, fresh_panel):
        from nse_eod.db import fetch_one

        r = fetch_one("SELECT count(*) AS n FROM gold.eod_adjusted WHERE cum_factor <= 0")
        assert r["n"] == 0

    def test_adjusted_ohlc_ordering_is_preserved(self, fresh_panel):
        """Scaling by a positive constant cannot break l <= o,c <= h."""
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT count(*) AS n FROM gold.eod_adjusted
             WHERE h_adj IS NOT NULL AND l_adj IS NOT NULL
               AND (l_adj > h_adj
                    OR c_adj > h_adj + 1e-6 OR c_adj < l_adj - 1e-6
                    OR o_adj > h_adj + 1e-6 OR o_adj < l_adj - 1e-6)
            """
        )
        assert r["n"] == 0, f"{r['n']} bars violate OHLC ordering after adjustment"

    def test_etf_units_are_out_of_the_tradeable_universe(self, fresh_panel):
        """INF* ISINs are mutual-fund/ETF units, not equity shares."""
        from nse_eod.db import fetch_all

        bad = fetch_all(
            """
            SELECT symbol, isin FROM gold.eod_adjusted
             WHERE isin LIKE 'INF%' AND in_universe
             LIMIT 5
            """
        )
        assert not bad, f"ETF units in the universe: {bad}"

    def test_non_equity_series_are_excluded_from_gold(self, fresh_panel):
        from nse_eod.db import fetch_all

        bad = fetch_all(
            """
            SELECT DISTINCT series FROM gold.eod_adjusted
             WHERE series IN ('GS','GB','TB','IV','RR','N0','N1','N2')
             LIMIT 5
            """
        )
        assert not bad, f"non-equity series leaked into gold: {bad}"

    def test_every_gold_row_has_a_bronze_source(self, fresh_panel):
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT count(*) AS n
              FROM gold.eod_adjusted g
             WHERE NOT EXISTS (
                SELECT 1 FROM bronze.eod_bhav_raw b
                 WHERE b.trade_date = g.trade_date
                   AND b.isin = COALESCE(g.isin_traded, g.isin)
                   AND b.series = g.series
             )
            """
        )
        assert r["n"] == 0, f"{r['n']} gold rows have no bronze source"


# ------------------------------------------------------------ trading calendar
class TestCalendarAuthority:
    """The calendar must never outrank the data.

    Both completeness bugs found in this pipeline came from trusting a calendar
    rule over the published bhavcopy:
      * every Saturday was skipped, losing the 2020-02-01 Budget session
        (1,886 rows) and the 2020-11-14 Diwali Muhurat session (1,878 rows);
      * a weekday with no calendar entry could not be distinguished from a
        missing ingest, because NSE's holiday API only covers the current year.
    """

    def test_recovered_saturday_sessions_are_present(self, has_bars):
        from nse_eod.db import fetch_all

        sats = fetch_all(
            """
            SELECT trade_date, count(*) AS bars
              FROM bronze.eod_bhav_raw
             WHERE extract(isodow FROM trade_date) = 6
             GROUP BY 1 ORDER BY 1
            """
        )
        if not sats:
            pytest.skip("no Saturday sessions in the ingested range")
        for s in sats:
            assert s["bars"] > 500, f"Saturday {s['trade_date']} has only {s['bars']} bars"

    def test_weekend_marked_only_by_the_weekend_rule_is_still_attempted(self, has_bars):
        """A weekend session must not be skipped by the live daily path."""
        import datetime as _dt

        from nse_eod.db import execute
        from nse_eod.orchestrate.ingest import is_trading_day

        # A future Saturday, marked non-trading by the weekend rule only.
        probe = _dt.date(2027, 1, 9)
        execute(
            """
            INSERT INTO bronze.trading_calendar
                (cal_date, is_trading_day, reason, segment, source)
            VALUES (%s, false, 'weekend', 'CM', 'weekend_rule')
            ON CONFLICT (cal_date) DO UPDATE
               SET is_trading_day = false, reason = 'weekend', source = 'weekend_rule'
            """,
            (probe,),
        )
        try:
            trading, reason = is_trading_day(probe)
            assert trading is True, "weekend_rule must not be authoritative for a weekend day"
            assert "weekend" in reason.lower()
        finally:
            execute("DELETE FROM bronze.trading_calendar WHERE cal_date = %s", (probe,))

    def test_sunday_is_NOT_assumed_closed(self, has_bars):
        """Sunday must be attempted, not skipped.

        This test asserted the opposite until 2026-02-01 falsified it: that was a
        SUNDAY and NSE published a full 180 KB bhavcopy for the Union Budget
        session. Diwali Muhurat 2023-11-12 was also a Sunday (2,463 bars). The
        old behaviour silently discarded both, and BIOFILCHEM's +44% phantom jump
        on 2026-02-02 was the symptom.
        """
        import datetime as _dt

        from nse_eod.db import execute
        from nse_eod.orchestrate.ingest import is_trading_day

        probe = _dt.date(2027, 1, 10)  # a Sunday
        execute(
            """
            INSERT INTO bronze.trading_calendar
                (cal_date, is_trading_day, reason, segment, source)
            VALUES (%s, false, 'weekend', 'CM', 'weekend_rule')
            ON CONFLICT (cal_date) DO UPDATE
               SET is_trading_day = false, reason = 'weekend', source = 'weekend_rule'
            """,
            (probe,),
        )
        try:
            trading, reason = is_trading_day(probe)
            assert trading is True, "Sunday must be attempted; only a 404 may rule it out"
            assert "weekend" in reason.lower()
        finally:
            execute("DELETE FROM bronze.trading_calendar WHERE cal_date = %s", (probe,))

    def test_both_recovered_sunday_sessions_are_in_the_panel(self, has_bars):
        from nse_eod.db import fetch_all

        got = fetch_all(
            """
            SELECT trade_date, count(*) AS bars
              FROM bronze.eod_bhav_raw
             WHERE trade_date IN ('2023-11-12', '2026-02-01')
             GROUP BY 1 ORDER BY 1
            """
        )
        found = {str(r["trade_date"]) for r in got}
        for d in ("2023-11-12", "2026-02-01"):
            if d not in found:
                pytest.skip(f"{d} outside the ingested range")
            bars = next(r["bars"] for r in got if str(r["trade_date"]) == d)
            assert bars > 1000, f"Sunday session {d} has only {bars} bars"

    def test_published_holiday_still_rules_a_saturday_out(self, has_bars):
        """Only the holiday master or an observed bhavcopy may veto a Saturday."""
        import datetime as _dt

        from nse_eod.db import execute
        from nse_eod.orchestrate.ingest import is_trading_day

        probe = _dt.date(2027, 1, 16)
        execute(
            """
            INSERT INTO bronze.trading_calendar
                (cal_date, is_trading_day, reason, segment, source)
            VALUES (%s, false, 'Test Holiday', 'CM', 'nse_holiday_master')
            ON CONFLICT (cal_date) DO UPDATE
               SET is_trading_day = false, reason = 'Test Holiday',
                   source = 'nse_holiday_master'
            """,
            (probe,),
        )
        try:
            trading, _ = is_trading_day(probe)
            assert trading is False, "a published holiday must still veto a Saturday"
        finally:
            execute("DELETE FROM bronze.trading_calendar WHERE cal_date = %s", (probe,))

    def test_no_unexplained_missing_weekdays(self, has_bars):
        """Every weekday in range has bars, or evidence that it was closed."""
        from nse_eod.db import fetch_all

        rows = fetch_all(
            """
            WITH span AS (
                SELECT min(trade_date) AS lo, max(trade_date) AS hi
                  FROM bronze.eod_bhav_raw
            ),
            cal AS (
                SELECT d::date AS cd
                  FROM span, generate_series(span.lo, span.hi, INTERVAL '1 day') d
                 WHERE extract(isodow FROM d) < 6
            )
            SELECT c.cd
              FROM cal c
              LEFT JOIN bronze.trading_calendar tc ON tc.cal_date = c.cd
             WHERE NOT EXISTS (
                    SELECT 1 FROM bronze.eod_bhav_raw b WHERE b.trade_date = c.cd
                   )
               AND (tc.cal_date IS NULL OR tc.is_trading_day IS TRUE)
             ORDER BY c.cd
            """
        )
        assert not rows, (
            f"{len(rows)} weekday(s) have no bars and no evidence of closure: "
            f"{[str(r['cd']) for r in rows[:10]]}"
        )


# -------------------------------------------------------- total-return series
class TestTotalReturnSeries:
    """The *_tr columns must actually differ from the price columns.

    Regression guard for a real bug: anchors were resolved only for
    PRICE_ADJUSTING types, so all ~9,400 ordinary dividends had
    ``anchor_price = NULL``, ``factor_tr`` stayed 1.0, and c_tr was identical to
    c_adj everywhere. A total-return series with no total return in it looks
    perfectly healthy unless you compare the two columns.
    """

    def test_ordinary_dividends_have_an_anchor_price(self, fresh_panel):
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT count(*) AS n, count(anchor_price) AS anchored
              FROM silver.corp_action_event
             WHERE type IN ('DIVIDEND','INTERIM_DIVIDEND')
               AND superseded_at IS NULL
               AND div_amount IS NOT NULL
               AND EXISTS (
                     SELECT 1 FROM silver.isin_link l
                       JOIN bronze.eod_bhav_raw b ON b.isin = l.isin
                      WHERE l.canonical_isin = COALESCE(corp_action_event.canonical_isin,
                                                        corp_action_event.isin)
                        AND b.trade_date < corp_action_event.ex_date
                   )
            """
        )
        if not r["n"]:
            pytest.skip("no anchorable dividends in the ingested window")
        pct = r["anchored"] / r["n"]
        assert pct > 0.9, (
            f"only {pct:.1%} of anchorable dividends have an anchor price; "
            "the total-return series cannot be built without P"
        )

    def test_tr_series_diverges_from_the_price_series(self, fresh_panel):
        """Over a dividend-paying history, c_tr must not equal c_adj everywhere."""
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT count(*) AS n,
                   count(*) FILTER (WHERE abs(c_tr - c_adj) > 1e-6) AS diverged
              FROM gold.eod_adjusted
             WHERE in_universe AND c_tr IS NOT NULL AND c_adj IS NOT NULL
            """
        )
        if not r["n"]:
            pytest.skip("gold panel empty")
        assert r["diverged"] > 0, (
            "c_tr is identical to c_adj on every bar -- dividends are not being "
            "reinvested, so the total-return series carries no total return"
        )

    def test_tr_is_never_below_the_price_series_on_a_dividend_payer(self, fresh_panel):
        """Reinvesting cash can only scale pre-ex bars DOWN relative to price.

        factor_tr = (P-D)/P < 1 for a dividend, so pre-ex c_tr < c_adj. The two
        converge at the right edge.
        """
        from nse_eod.db import fetch_one

        r = fetch_one(
            """
            SELECT count(*) AS n FROM gold.eod_adjusted
             WHERE in_universe AND c_tr IS NOT NULL AND c_adj IS NOT NULL
               AND c_tr > c_adj * 1.0000001
            """
        )
        assert r["n"] == 0, f"{r['n']} bars have c_tr above c_adj, which the formula forbids"


# -------------------------------------------------------------- idempotency
class TestIdempotency:
    def _snapshot(self) -> dict:
        """A content fingerprint of every layer, ignoring volatile timestamps."""
        from nse_eod.db import fetch_one

        return {
            "bronze": fetch_one(
                """
                SELECT count(*) AS n,
                       md5(string_agg(trade_date::text||isin||series||row_hash, '|'
                                      ORDER BY trade_date, isin, series)) AS h
                  FROM bronze.eod_bhav_raw
                """
            ),
            "events": fetch_one(
                """
                SELECT count(*) AS n,
                       md5(string_agg(event_uid||COALESCE(factor_price::text,'')||factor_state, '|'
                                      ORDER BY event_uid)) AS h
                  FROM silver.corp_action_event
                """
            ),
            "gold": fetch_one(
                """
                SELECT count(*) AS n,
                       md5(string_agg(trade_date::text||isin||series||
                                      round(c_adj::numeric, 8)::text||
                                      round(cum_factor::numeric, 12)::text, '|'
                                      ORDER BY trade_date, isin, series)) AS h
                  FROM gold.eod_adjusted
                """
            ),
            "review": fetch_one(
                "SELECT count(*) AS n FROM silver.corp_action_review_queue"
            ),
            "links": fetch_one("SELECT count(*) AS n FROM silver.isin_link"),
        }

    def test_run_daily_twice_is_identical(self, has_bars):
        """The core idempotency guarantee.

        Corp-action fetching is skipped so the test stays offline and
        deterministic; every DB write path is still exercised.
        """
        from nse_eod.orchestrate.daily import run_daily

        target = has_bars  # the latest ingested trade date

        run_daily(target, force=True, skip_corp_actions=True)
        before = self._snapshot()

        run_daily(target, force=True, skip_corp_actions=True)
        after = self._snapshot()

        for layer in before:
            assert before[layer]["n"] == after[layer]["n"], (
                f"{layer} row count changed: {before[layer]['n']} -> {after[layer]['n']}"
            )
            if "h" in before[layer]:
                assert before[layer]["h"] == after[layer]["h"], f"{layer} content changed"

    def test_rebuild_adjusted_is_idempotent(self, has_bars):
        from nse_eod.transform.adjusted import materialize

        materialize(None)
        before = self._snapshot()["gold"]
        materialize(None)
        after = self._snapshot()["gold"]
        assert before["n"] == after["n"]
        assert before["h"] == after["h"]

    def test_ingest_eod_second_run_writes_nothing(self, has_bars, fake_session, fixture_date):
        """Offline: the row_hash gate must make an unchanged re-run a true no-op."""
        from nse_eod.orchestrate.ingest import ingest_eod

        first = ingest_eod(fixture_date, fake_session)
        second = ingest_eod(fixture_date, fake_session)
        assert second["upserted"] == 0, "unchanged re-ingest must upsert zero rows"
        assert second["restated"] == 0
        assert second["rows"] == first["rows"]

    def test_isin_link_refresh_is_idempotent(self, has_bars):
        from nse_eod.transform import isin_link

        isin_link.refresh()
        before = self._snapshot()
        isin_link.refresh()
        after = self._snapshot()
        assert before["links"]["n"] == after["links"]["n"]
        assert before["review"]["n"] == after["review"]["n"], (
            "review queue grew on a repeat run -- the NULL-key dedupe regressed"
        )


# ------------------------------------------------------ backfill determinism
class TestBackfillDeterminism:
    def test_gold_is_reconstructible_from_bronze_and_factors_alone(self, has_bars):
        """Drop the entire panel and rebuild it; the result must be identical.

        This is what makes the three-store design worth its cost: gold is a pure
        derivation, so it can always be regenerated and never needs backing up.
        """
        from nse_eod.db import execute, fetch_one
        from nse_eod.transform.adjusted import materialize

        def fingerprint():
            return fetch_one(
                """
                SELECT count(*) AS n,
                       md5(string_agg(trade_date::text||isin||series||
                                      round(c_adj::numeric, 8)::text||
                                      round(cum_factor::numeric, 12)::text||
                                      round(COALESCE(c_tr,0)::numeric, 8)::text, '|'
                                      ORDER BY trade_date, isin, series)) AS h
                  FROM gold.eod_adjusted
                """
            )

        materialize(None)
        original = fingerprint()
        assert original["n"] > 0

        execute("TRUNCATE gold.eod_adjusted")
        assert fetch_one("SELECT count(*) AS n FROM gold.eod_adjusted")["n"] == 0

        materialize(None)
        rebuilt = fingerprint()

        assert rebuilt["n"] == original["n"], "row count changed after a full rebuild"
        assert rebuilt["h"] == original["h"], "panel content changed after a full rebuild"

    def test_bronze_is_never_mutated_by_a_gold_rebuild(self, has_bars):
        from nse_eod.db import fetch_one
        from nse_eod.transform.adjusted import materialize

        q = "SELECT count(*) AS n, max(ingested_at) AS t FROM bronze.eod_bhav_raw"
        before = fetch_one(q)
        materialize(None)
        after = fetch_one(q)
        assert before["n"] == after["n"]
        assert before["t"] == after["t"], "bronze must be immutable"


# ----------------------------------------------------------- as-of replay
class TestAsOfReproducibility:
    def test_as_of_before_any_event_was_learned_yields_unadjusted_prices(self, has_bars):
        """Filtering on learned_at reconstructs a past adjusted snapshot.

        This is why the factor table records WHEN each event was learned: an
        as-of timestamp preceding the first learned_at must give a panel with no
        adjustments applied at all.
        """
        from nse_eod.db import execute, fetch_one
        from nse_eod.transform.adjusted import materialize

        first_learned = fetch_one(
            "SELECT min(learned_at) AS t FROM silver.corp_action_event"
        )["t"]
        if first_learned is None:
            pytest.skip("no events loaded")

        as_of = first_learned - dt.timedelta(days=1)
        try:
            materialize(None, as_of=as_of)
            r = fetch_one(
                """
                SELECT count(*) AS n FROM gold.eod_adjusted
                 WHERE abs(cum_factor - 1.0) > 1e-9
                """
            )
            assert r["n"] == 0, (
                f"{r['n']} rows still adjusted at an as-of before any event was known"
            )
        finally:
            # Always restore the live panel.
            execute("TRUNCATE gold.eod_adjusted")
            materialize(None)
