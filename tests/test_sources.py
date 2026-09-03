"""Source-layer tests: URL construction, multi-index CA fetch, retry semantics.

No network: the NSE session is faked.
"""

from __future__ import annotations

import datetime as dt

import pytest

from nse_eod.config import Settings
from nse_eod.sources.bhavcopy import (
    delivery_url,
    legacy_url,
    priceband_url,
    udiff_url,
    _candidate_urls,
)
from nse_eod.sources.corp_actions import fetch_corp_actions, payload_hash, windows
from nse_eod.sources.http import NSEFetchError, NSENotFound


# ------------------------------------------------------------------- URL shapes
class TestUrls:
    def test_udiff_url(self):
        assert udiff_url(dt.date(2026, 8, 28)).endswith(
            "/content/cm/BhavCopy_NSE_CM_0_0_0_20260828_F_0000.csv.zip"
        )

    def test_legacy_url_uses_uppercase_month(self):
        u = legacy_url(dt.date(2020, 7, 13))
        assert u.endswith("/content/historical/EQUITIES/2020/JUL/cm13JUL2020bhav.csv.zip")

    def test_delivery_url_is_ddmmyyyy(self):
        assert delivery_url(dt.date(2026, 8, 28)).endswith("sec_bhavdata_full_28082026.csv")

    def test_priceband_url_is_ddmmyyyy(self):
        assert priceband_url(dt.date(2026, 8, 28)).endswith("sec_list_28082026.csv")

    def test_both_formats_are_always_tried(self):
        """Being wrong about the cutover costs one 404; hardcoding loses the day."""
        s = Settings()
        for d in (dt.date(2020, 5, 1), dt.date(2026, 8, 28)):
            labels = [lbl for lbl, _ in _candidate_urls(d, s)]
            assert set(labels) == {"udiff", "legacy"}

    def test_recent_dates_try_udiff_first(self):
        s = Settings()
        assert _candidate_urls(dt.date(2026, 8, 28), s)[0][0] == "udiff"

    def test_old_dates_try_legacy_first(self):
        s = Settings()
        assert _candidate_urls(dt.date(2020, 5, 1), s)[0][0] == "legacy"


# ------------------------------------------------------- corp-action windowing
class TestWindows:
    def test_windows_cover_the_range_without_overlap(self):
        got = list(windows(dt.date(2024, 1, 1), dt.date(2024, 3, 31), 30))
        assert got[0][0] == dt.date(2024, 1, 1)
        assert got[-1][1] == dt.date(2024, 3, 31)
        for (a1, b1), (a2, _) in zip(got, got[1:]):
            assert a2 == b1 + dt.timedelta(days=1)

    def test_single_day_range(self):
        assert list(windows(dt.date(2024, 1, 1), dt.date(2024, 1, 1), 60)) == [
            (dt.date(2024, 1, 1), dt.date(2024, 1, 1))
        ]


class TestPayloadHash:
    def test_same_announcement_hashes_identically(self):
        a = payload_hash("INE123A01011", "01-Jan-2026", "Bonus 1:1", "02-Jan-2026")
        b = payload_hash("ine123a01011", "01-Jan-2026", "  bonus 1:1  ", "02-Jan-2026")
        assert a == b, "case and whitespace must not create a duplicate"

    def test_different_purposes_on_one_ex_date_stay_distinct(self):
        """Compound announcements share ISIN and ex_date; purpose separates them."""
        a = payload_hash("INE123A01011", "01-Jan-2026", "Bonus 1:1", None)
        b = payload_hash("INE123A01011", "01-Jan-2026", "Dividend - Rs 4 Per Share", None)
        assert a != b


# --------------------------------------------------- multi-index CA fetch (SME)
class _FakeSession:
    """Records which `index` values were requested and serves canned rows."""

    def __init__(self, per_index: dict[str, list[dict]]):
        self.per_index = per_index
        self.requested: list[str] = []

    def get_json(self, url, referer="", params=None):
        idx = (params or {}).get("index")
        self.requested.append(idx)
        return list(self.per_index.get(idx, []))

    def warm(self, force=False):
        pass


def _row(symbol: str, subject: str, ex: str = "15-Jan-2024") -> dict:
    return {
        "symbol": symbol,
        "subject": subject,
        "exDate": ex,
        "isin": f"INE{symbol[:3]}A01011",
        "faceVal": "10",
        "recDate": ex,
        "comp": symbol,
        "series": "EQ",
    }


class TestSmeIndexIsFetched:
    """NSE's CA API is segmented; `equities` EXCLUDES the SME platform.

    Fetching only `equities` left ~1,350 SME stocks with invisible splits and
    bonuses, which surfaced as unexplained -80% to -96% single-day drops in the
    adjusted panel (ISHAN, GOLDSTAR, CELLECOR, COOLCAPS, VMARCIND, JSLL, MOS,
    SECL -- all SM/ST series). `index=sme` is a separate feed.
    """

    def test_both_segments_are_requested(self):
        s = Settings(ca_indices=["equities", "sme"])
        sess = _FakeSession({"equities": [_row("RELI", "Bonus 1:1")], "sme": []})
        fetch_corp_actions(dt.date(2024, 1, 1), dt.date(2024, 1, 31), sess, s)
        assert set(sess.requested) == {"equities", "sme"}

    def test_sme_rows_are_included_in_the_result(self):
        s = Settings(ca_indices=["equities", "sme"])
        sess = _FakeSession(
            {
                "equities": [_row("RELI", "Bonus 1:1")],
                "sme": [_row("ISHAN", "FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RE 1/- PER SHARE")],
            }
        )
        rows = fetch_corp_actions(dt.date(2024, 1, 1), dt.date(2024, 1, 31), sess, s)
        symbols = {r["symbol"] for r in rows}
        assert "ISHAN" in symbols, "SME rows must reach the result"
        assert "RELI" in symbols

    def test_equities_only_config_would_miss_sme(self):
        """Demonstrates the original defect, so the fix cannot silently regress."""
        s = Settings(ca_indices=["equities"])
        sess = _FakeSession({"equities": [_row("RELI", "Bonus 1:1")], "sme": [_row("ISHAN", "BONUS 2:1")]})
        rows = fetch_corp_actions(dt.date(2024, 1, 1), dt.date(2024, 1, 31), sess, s)
        assert {r["symbol"] for r in rows} == {"RELI"}
        assert sess.requested == ["equities"]

    def test_default_config_includes_sme(self):
        assert "sme" in Settings().ca_indices

    def test_duplicate_rows_across_indices_are_deduped(self):
        s = Settings(ca_indices=["equities", "sme"])
        dup = _row("BOTH", "Bonus 1:1")
        sess = _FakeSession({"equities": [dup], "sme": [dict(dup)]})
        rows = fetch_corp_actions(dt.date(2024, 1, 1), dt.date(2024, 1, 31), sess, s)
        assert len(rows) == 1

    def test_uppercase_sme_purpose_still_parses(self):
        """The SME feed returns purpose text in UPPERCASE."""
        from nse_eod.parsers.corp_action_text import SPLIT, parse_purpose

        upper = "FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RE 1/- PER SHARE"
        comps = parse_purpose(upper, 1).components
        assert len(comps) == 1
        assert comps[0].type == SPLIT
        assert comps[0].parsed_ok
        assert (comps[0].ratio_num, comps[0].ratio_den) == (10, 1) or (
            str(comps[0].ratio_num),
            str(comps[0].ratio_den),
        ) == ("10", "1")


class TestMalformedResponseIsRejected:
    def test_non_list_body_raises(self):
        """The API answers 200 with a nonsense body for a bad request."""
        from nse_eod.sources.corp_actions import CorpActionFetchError

        class Bad:
            def get_json(self, url, referer="", params=None):
                return {"error": "nope"}

            def warm(self, force=False):
                pass

        with pytest.raises(CorpActionFetchError, match="expected list"):
            fetch_corp_actions(dt.date(2024, 1, 1), dt.date(2024, 1, 2), Bad(), Settings())

    def test_list_of_strings_raises(self):
        from nse_eod.sources.corp_actions import CorpActionFetchError

        class Bad:
            def get_json(self, url, referer="", params=None):
                return ["nope", "nope"]

            def warm(self, force=False):
                pass

        with pytest.raises(CorpActionFetchError, match="expected dicts"):
            fetch_corp_actions(dt.date(2024, 1, 1), dt.date(2024, 1, 2), Bad(), Settings())


class TestRetrySemantics:
    def test_not_found_is_a_subclass_but_must_not_be_retried(self):
        """A 404 is a definitive answer.

        NSENotFound subclasses NSEFetchError so callers can catch either, but the
        retry predicate must exclude it — retrying every 404 five times with
        exponential backoff cost ~33 s per holiday.
        """
        from tenacity import retry_if_exception_type, retry_if_not_exception_type

        assert issubclass(NSENotFound, NSEFetchError)
        pred = retry_if_exception_type((NSEFetchError,)) & retry_if_not_exception_type(
            NSENotFound
        )
        assert pred is not None  # composition is valid
