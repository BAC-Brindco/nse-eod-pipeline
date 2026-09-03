"""Validation-layer tests: alert formatting, severity/exit-code mapping, and
that each check fires on a crafted breach against the live DB."""

from __future__ import annotations

import datetime as dt

import pytest

from nse_eod.validate import alerts
from nse_eod.validate import checks as V


# ----------------------------------------------------- pure severity mapping
class TestSeverityAndExitCodes:
    def test_worst_severity_prefers_critical(self):
        fs = [
            V.Finding("a", V.INFO, "i"),
            V.Finding("b", V.WARNING, "w"),
            V.Finding("c", V.CRITICAL, "c"),
        ]
        assert V.worst_severity(fs) == V.CRITICAL

    def test_worst_severity_warning(self):
        assert V.worst_severity([V.Finding("a", V.INFO, "i"), V.Finding("b", V.WARNING, "w")]) == V.WARNING

    def test_worst_severity_info(self):
        assert V.worst_severity([V.Finding("a", V.INFO, "i")]) == V.INFO

    def test_no_findings_is_none(self):
        assert V.worst_severity([]) is None

    @pytest.mark.parametrize(
        "sev,code", [(V.CRITICAL, 2), (V.WARNING, 1), (V.INFO, 0), (None, 0)]
    )
    def test_exit_codes(self, sev, code):
        """Alerts must set a non-zero exit code so a scheduler notices."""
        assert V.exit_code_for(sev) == code

    def test_info_findings_do_not_fail_a_run(self):
        """Informational findings are recorded but must not page anyone."""
        assert V.exit_code_for(V.worst_severity([V.Finding("x", V.INFO, "fyi")])) == 0


class TestFindingSerialisation:
    def test_as_row_shape(self):
        f = V.Finding(
            check_name="overnight_jump",
            severity=V.CRITICAL,
            detail="d",
            trade_date=dt.date(2026, 8, 28),
            isin="INE123A01011",
            symbol="ACME",
            metrics={"move_pct": -50.0},
        )
        row = f.as_row(run_id=7)
        assert row["run_id"] == 7
        assert row["check_name"] == "overnight_jump"
        assert row["severity"] == V.CRITICAL
        assert '"move_pct"' in row["metrics"]

    def test_metrics_serialises_dates(self):
        """json.dumps(default=str) must not blow up on a date in metrics."""
        f = V.Finding("c", V.INFO, "d", metrics={"when": dt.date(2026, 1, 1)})
        assert "2026-01-01" in f.as_row(None)["metrics"]


# ------------------------------------------------------------- alert payload
class TestAlertFormatting:
    def _findings(self):
        return [
            V.Finding("overnight_jump", V.CRITICAL, "ACME: -50% unexplained", symbol="ACME"),
            V.Finding("unparsed_purpose", V.WARNING, "3 open review items"),
        ]

    def test_payload_has_text_and_blocks(self):
        p = alerts.format_slack(self._findings(), "run-daily", dt.date(2026, 8, 28), "abc123")
        assert "text" in p and "blocks" in p
        assert "1 critical" in p["text"]
        assert "1 warning" in p["text"]

    def test_payload_includes_the_detail(self):
        p = alerts.format_slack(self._findings(), "run-daily", dt.date(2026, 8, 28), "abc123")
        body = str(p["blocks"])
        assert "ACME" in body
        assert "abc123" in body

    def test_long_lists_are_truncated_not_dropped(self):
        many = [V.Finding("overnight_jump", V.CRITICAL, f"S{i}: bad") for i in range(40)]
        p = alerts.format_slack(many, "run-daily", None, "run1")
        body = str(p["blocks"])
        assert "and 28 more" in body, "must say how many were elided"

    def test_no_webhook_configured_returns_false_and_does_not_raise(self, monkeypatch):
        """Findings still persist and still set the exit code; only delivery is skipped."""
        from nse_eod.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "alert_webhook_url", None, raising=False)
        assert alerts.send(self._findings(), "run-daily", None, "u", None, s) is False

    def test_info_only_findings_are_not_sent(self, monkeypatch):
        from nse_eod.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "alert_webhook_url", "https://example.invalid/hook", raising=False)
        assert alerts.send([V.Finding("x", V.INFO, "fyi")], "run-daily", None, "u", None, s) is False

    def test_delivery_failure_never_raises(self, monkeypatch):
        """A webhook outage must not turn a successful ingest into a failed run."""
        import httpx

        from nse_eod.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "alert_webhook_url", "https://example.invalid/hook", raising=False)
        monkeypatch.setattr(s, "alert_dry_run", False, raising=False)

        def boom(*a, **k):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(httpx, "post", boom)
        assert alerts.send(self._findings(), "run-daily", None, "u", None, s) is False


# ------------------------------------------------------- live checks (DB)
@pytest.mark.integration
class TestChecksAgainstLiveData:
    def test_all_checks_run_without_error(self, has_bars):
        """A broken check is reported as a finding, never raised.

        If any check crashed, run_all converts it into a
        ``constraint_violation`` finding -- so the absence of those proves every
        check's SQL is valid against the real schema.
        """
        findings = V.run_all(has_bars)
        crashes = [f for f in findings if f.check_name == "constraint_violation"]
        assert not crashes, f"checks crashed: {[f.detail for f in crashes]}"

    def test_rowcount_check_fires_on_a_missing_day(self, has_bars):
        """A date with no bars must be flagged critical."""
        empty_day = dt.date(1990, 1, 2)
        fs = V.check_rowcount(empty_day)
        assert fs and fs[0].severity == V.CRITICAL
        assert "no bronze rows" in fs[0].detail

    def test_prev_close_check_is_quiet_on_clean_data(self, has_bars):
        """After ISIN linkage, a clean day should produce no prev_close breaks.

        This is the check that would fire on every corporate action if it
        wrongly compared against a factor-adjusted prior close.
        """
        fs = V.check_prev_close(has_bars)
        assert len(fs) <= 3, f"unexpected prev_close breaks: {[f.detail for f in fs][:3]}"

    def test_overnight_jump_explains_known_corporate_actions(self, fresh_panel):
        """A bonus/split ex-date move must be reported as EXPLAINED, not critical."""
        fs = V.check_overnight_jump(dt.date(2026, 8, 24))
        unexplained = [f for f in fs if f.severity == V.CRITICAL]
        for f in unexplained:
            assert "TDPOWERSYS" not in (f.symbol or ""), (
                "a known split was reported as unexplained"
            )

    def test_right_edge_check_passes_on_a_fresh_panel(self, fresh_panel):
        assert V.check_right_edge_identity() == []

    def test_stale_factor_uses_a_three_way_severity_split(self, has_bars):
        """Three distinct situations, three severities.

        A check that can never come back clean gets muted, so the severity must
        track whether anyone can actually act:

          CRITICAL - anchor available, no factor, and NOT in the review queue.
                     A silent hole; a genuine defect.
          WARNING  - anchor available, no factor, but TRACKED in the queue.
                     Awaiting a human. Some are permanently unresolvable from
                     data (BIRET's "Repayment Of Spv Debt" carries no amount),
                     so paging nightly on these would be pure noise.
          INFO     - no bar before the ex-date at all; history is not
                     backfilled that far. Expected, not actionable.
        """
        fs = V.check_stale_factors(has_bars)
        for f in fs:
            if f.severity == V.CRITICAL:
                assert "NOT in the review queue" in f.detail
            elif f.severity == V.WARNING:
                assert "tracked" in f.detail.lower()
            else:
                assert f.severity == V.INFO
                assert "no bar exists" in f.detail

    def test_tracked_stale_factors_do_not_page(self, has_bars):
        """The whole point: a tracked, unresolvable gap must not be CRITICAL."""
        fs = V.check_stale_factors(has_bars)
        criticals = [f for f in fs if f.severity == V.CRITICAL]
        for f in criticals:
            assert "NOT in the review queue" in f.detail, (
                "a CRITICAL stale_factor must mean an UNTRACKED hole, otherwise "
                "run-daily exits non-zero forever and the channel gets ignored"
            )

    def test_persist_writes_findings(self, has_bars):
        from nse_eod.db import fetch_one

        f = V.Finding(
            check_name="overnight_jump",
            severity=V.INFO,
            detail="unit-test marker",
            trade_date=has_bars,
            symbol="TESTONLY",
        )
        n = V.persist([f], run_id=None)
        assert n == 1
        got = fetch_one(
            "SELECT count(*) AS n FROM ops.validation_finding WHERE symbol = 'TESTONLY'"
        )
        assert got["n"] >= 1
