"""NSE HTTP client: cookie warm-up, browser headers, retry/backoff, rate limiting.

Behaviour encoded here was measured against live NSE on 2026-08-31:

* ``nsearchives.nseindia.com`` (bhavcopy, delivery, price bands, EQUITY_L) serves
  **without any cookie** given a browser User-Agent and a Referer.
* ``www.nseindia.com/api/*`` (corp actions, holiday master) requires a cookie jar.
  The homepage itself answered **403 while still setting the AKA_A2 cookie**; a
  follow-up GET of a real content page raised the jar to 6 cookies and the API
  then returned 200. So a 403 during warm-up is *not* a failure and must not
  abort the run.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

ARCHIVES = "https://nsearchives.nseindia.com"
WWW = "https://www.nseindia.com"

# Pages whose only job is to populate the cookie jar.
WARMUP_PAGES = (
    f"{WWW}/",
    f"{WWW}/companies-listing/corporate-filings-actions",
)


class NSEFetchError(RuntimeError):
    """Raised when a URL cannot be fetched after all retries."""


class NSENotFound(NSEFetchError):
    """404 — the file genuinely does not exist (holiday, or before the format began)."""


class _RateLimiter:
    """Simple thread-safe minimum-interval gate."""

    def __init__(self, per_second: float):
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()


class NSESession:
    """A cookie-warming, rate-limited, retrying NSE client.

    Injected into every source module so unit tests can substitute a fake and
    never touch the network.
    """

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.s = settings or get_settings()
        self._limiter = _RateLimiter(self.s.http_rate_limit_per_s)
        self._warmed = False
        self._client = client or httpx.Client(
            timeout=self.s.http_timeout_s,
            follow_redirects=True,
            headers=self._base_headers(),
        )

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.s.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            # Advertise ONLY what httpx decodes natively. Including "br" makes
            # NSE return Brotli, which httpx cannot inflate without the optional
            # brotli package -- the body then arrives as raw compressed bytes and
            # json()/decode() fails with "invalid continuation byte".
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    # ------------------------------------------------------------- warm-up
    def warm(self, force: bool = False) -> None:
        """Populate the cookie jar. Tolerates the 403-with-cookie the homepage returns."""
        if self._warmed and not force:
            return
        for url in WARMUP_PAGES:
            self._limiter.wait()
            try:
                r = self._client.get(url)
                log.debug(
                    "nse_warmup", url=url, status=r.status_code, cookies=len(self._client.cookies)
                )
            except httpx.HTTPError as exc:
                log.warning("nse_warmup_failed", url=url, error=str(exc))
        # Any cookie at all is enough to proceed; the API call itself is the real test.
        self._warmed = len(self._client.cookies) > 0
        if not self._warmed:
            log.warning("nse_warmup_no_cookies")

    def reset(self) -> None:
        self._client.cookies.clear()
        self._warmed = False

    # --------------------------------------------------------------- fetch
    def _get(self, url: str, referer: str, params: dict | None = None) -> httpx.Response:
        self._limiter.wait()
        headers = {"Referer": referer}
        r = self._client.get(url, headers=headers, params=params)
        if r.status_code == 404:
            raise NSENotFound(f"404 {url}")
        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            # Session likely stale or we are being throttled: re-warm then let
            # tenacity retry with backoff.
            self.reset()
            self.warm(force=True)
            raise NSEFetchError(f"{r.status_code} {url}")
        r.raise_for_status()
        return r

    def get_bytes(self, url: str, referer: str = f"{WWW}/all-reports", params: dict | None = None) -> bytes:
        """Fetch raw bytes with retry. Used for archives (zip/csv)."""

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.s.http_max_retries),
            wait=wait_exponential_jitter(initial=self.s.http_backoff_base_s, max=30),
            # A 404 is a DEFINITIVE answer -- the file does not exist -- not a
            # transient failure. NSENotFound subclasses NSEFetchError, so a bare
            # retry_if_exception_type retried every 404 five times with
            # exponential backoff: ~33 s per holiday, which made probing a
            # multi-year range for Saturday sessions unusably slow.
            retry=(
                retry_if_exception_type((NSEFetchError, httpx.HTTPError))
                & retry_if_not_exception_type(NSENotFound)
            ),
        )
        def _attempt() -> bytes:
            r = self._get(url, referer=referer, params=params)
            return r.content

        try:
            content = _attempt()
        except NSENotFound:
            raise
        except Exception as exc:
            raise NSEFetchError(f"giving up on {url}: {exc}") from exc
        log.debug("nse_fetch_ok", url=url, bytes=len(content))
        return content

    def get_json(self, url: str, referer: str, params: dict | None = None):
        """Fetch JSON from ``www.nseindia.com/api/*``. Warms cookies first."""
        self.warm()

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.s.http_max_retries),
            wait=wait_exponential_jitter(initial=self.s.http_backoff_base_s, max=30),
            retry=(
                retry_if_exception_type((NSEFetchError, httpx.HTTPError, ValueError))
                & retry_if_not_exception_type(NSENotFound)
            ),
        )
        def _attempt():
            r = self._get(
                url,
                referer=referer,
                params=params,
            )
            return r.json()

        try:
            return _attempt()
        except NSENotFound:
            raise
        except Exception as exc:
            raise NSEFetchError(f"giving up on {url}: {exc}") from exc

    # ------------------------------------------------------- raw archiving
    def archive(self, content: bytes, name: str) -> Path | None:
        """Persist the raw downloaded file so bronze can always be re-derived offline."""
        if not self.s.keep_raw_files:
            return None
        p = self.s.raw_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NSESession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
