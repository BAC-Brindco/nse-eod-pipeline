"""Can a GitHub-hosted runner actually reach NSE? Answer it definitively.

WHY THIS EXISTS
---------------
The pipeline's entire job is scraping nseindia.com, and it already needs cookie
warm-up, browser headers and exponential backoff to work from a desk in India. NSE's
edge is known to treat datacenter and non-IN egress differently. Before any of this
pipeline is ported to GitHub Actions, we need to know whether the runner can fetch
the files at all -- guessing would mean discovering it on the first night the data
silently stopped arriving.

WHAT "SOLID" MEANS FOR A PROBE
------------------------------
1. Test the REAL endpoints the pipeline uses, not a homepage ping. Each has a
   different edge posture: nsearchives serves files with no cookie, www/api needs a
   cookie jar and returns 403 during warm-up as a matter of course.
2. Report the egress IP and its geography, because that is the variable that decides
   the outcome and it differs between runner pools.
3. Distinguish the outcomes that look alike:
      403 / 401  -> actively blocked
      404        -> not published yet (a TIMING answer, not a blocking one)
      200        -> reachable
      timeout    -> throttled or black-holed
   A run that reports "failed" without separating these is useless.
4. Try each endpoint twice, once cold and once after warm-up, so we learn whether
   the cookie dance works from a runner or only from a residential IP.
5. NEVER fail the workflow because NSE said no. A block is a FINDING. Exiting
   non-zero would make the answer look like broken CI and invite someone to "fix"
   the probe.

The probe writes a verdict to the job summary and a machine-readable artifact.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

ARCHIVES = "https://nsearchives.nseindia.com"
WWW = "https://www.nseindia.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    # Deliberately NOT advertising br: the pipeline learned this the hard way --
    # httpx/urllib cannot inflate brotli and the body arrives as binary noise.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 25


def recent_weekdays(n: int = 6) -> list[dt.date]:
    """The last `n` weekdays, newest first. Today's file may not be published yet,
    so the probe walks back until it finds one rather than judging on a single 404."""
    out: list[dt.date] = []
    d = dt.date.today()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


def egress() -> dict:
    """Who does NSE see us as? This is the variable that decides everything."""
    info: dict = {}
    for url, keys in (
        ("https://ifconfig.co/json", ("ip", "country", "asn_org", "city")),
        ("https://ipinfo.io/json", ("ip", "country", "org", "city")),
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=12) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            for k in keys:
                if j.get(k) and k not in info:
                    info[k] = j[k]
            if info.get("ip"):
                break
        except Exception as e:  # noqa: BLE001
            info.setdefault("lookup_errors", []).append(f"{url}: {type(e).__name__}")
    return info


class Probe:
    def __init__(self) -> None:
        self.jar: list[str] = []
        self.ctx = ssl.create_default_context()

    def _headers(self, referer: str | None) -> dict[str, str]:
        h = dict(BASE_HEADERS)
        if referer:
            h["Referer"] = referer
        if self.jar:
            h["Cookie"] = "; ".join(self.jar)
        return h

    def fetch(self, url: str, referer: str | None = None, label: str = "") -> dict:
        t0 = time.time()
        rec: dict = {"label": label or url, "url": url}
        try:
            req = urllib.request.Request(url, headers=self._headers(referer))
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as r:
                body = r.read(4096)
                rec.update(
                    status=r.status,
                    bytes_read=len(body),
                    content_type=r.headers.get("Content-Type", ""),
                    server=r.headers.get("Server", ""),
                    outcome="reachable",
                )
                # Harvest cookies so the warm-up pass has something to send.
                for sc in r.headers.get_all("Set-Cookie") or []:
                    pair = sc.split(";", 1)[0]
                    if pair and pair not in self.jar:
                        self.jar.append(pair)
        except urllib.error.HTTPError as e:
            rec.update(
                status=e.code,
                bytes_read=0,
                server=e.headers.get("Server", "") if e.headers else "",
                outcome={
                    403: "BLOCKED",
                    401: "BLOCKED",
                    404: "not_published",
                    429: "RATE_LIMITED",
                    503: "unavailable",
                }.get(e.code, f"http_{e.code}"),
            )
            for sc in (e.headers.get_all("Set-Cookie") if e.headers else None) or []:
                pair = sc.split(";", 1)[0]
                if pair and pair not in self.jar:
                    self.jar.append(pair)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            rec.update(status=None, outcome="TIMEOUT_OR_DNS", error=f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            rec.update(status=None, outcome="ERROR", error=f"{type(e).__name__}: {e}")
        rec["elapsed_ms"] = int((time.time() - t0) * 1000)
        return rec


def main() -> int:
    days = recent_weekdays(6)
    p = Probe()
    results: list[dict] = []

    # ---- 1. archives: files, no cookie required. The equity + index feeds.
    for d in days[:4]:
        results.append(
            p.fetch(
                f"{ARCHIVES}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
                referer=f"{WWW}/all-reports",
                label=f"bhavcopy {d}",
            )
        )
        results.append(
            p.fetch(
                f"{ARCHIVES}/content/indices/ind_close_all_{d:%d%m%Y}.csv",
                referer=f"{WWW}/all-reports",
                label=f"index close {d}",
            )
        )
    results.append(
        p.fetch(f"{ARCHIVES}/content/equities/EQUITY_L.csv",
                referer=f"{WWW}/market-data/securities-available-for-trading",
                label="EQUITY_L.csv (no date dependency)")
    )

    # ---- 2. cookie warm-up, then the www/api surface that needs a jar.
    cold_api = p.fetch(f"{WWW}/api/holiday-master?type=trading",
                       referer=f"{WWW}/resources/exchange-communication-holidays",
                       label="api/holiday-master (COLD, no cookie)")
    results.append(cold_api)

    for warm in (WWW + "/", WWW + "/companies-listing/corporate-filings-actions"):
        results.append(p.fetch(warm, referer=WWW, label=f"warm-up {warm.rsplit('/', 1)[-1] or 'homepage'}"))

    warm_api = p.fetch(f"{WWW}/api/holiday-master?type=trading",
                       referer=f"{WWW}/resources/exchange-communication-holidays",
                       label="api/holiday-master (WARM, with cookie)")
    results.append(warm_api)

    # ------------------------------------------------------------------ verdict
    arch = [r for r in results if "nsearchives" in r["url"]]
    arch_ok = [r for r in arch if r.get("status") == 200]
    arch_blocked = [r for r in arch if r.get("outcome") in ("BLOCKED", "RATE_LIMITED")]
    api_ok = warm_api.get("status") == 200

    if arch_blocked:
        verdict = "BLOCKED"
        summary = (
            f"NSE actively blocked {len(arch_blocked)}/{len(arch)} archive requests from "
            "this runner. A GHA-hosted pipeline is NOT viable without an India-egress "
            "proxy or a self-hosted runner."
        )
    elif not arch_ok:
        verdict = "INCONCLUSIVE"
        summary = (
            "No archive request returned 200, but none was explicitly blocked either "
            "(all 404 / timeout). Either the files are not published for these dates "
            "or the runner is being black-holed. Re-run after 19:00 IST."
        )
    elif api_ok:
        verdict = "VIABLE"
        summary = (
            f"{len(arch_ok)}/{len(arch)} archive fetches returned 200 AND the cookie-gated "
            "API answered after warm-up. The full pipeline can run on GHA."
        )
    else:
        verdict = "PARTIAL"
        summary = (
            f"{len(arch_ok)}/{len(arch)} archive fetches returned 200, but the cookie-gated "
            "www/api surface did not answer even after warm-up. File-based ingest "
            "(bhavcopy, index close) would work on GHA; corporate actions and the "
            "holiday master would not."
        )

    report = {
        "probed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "runner": {
            "os": os.environ.get("RUNNER_OS", "?"),
            "arch": os.environ.get("RUNNER_ARCH", "?"),
            "name": os.environ.get("RUNNER_NAME", "?"),
        },
        "egress": egress(),
        "verdict": verdict,
        "summary": summary,
        "cookies_collected": len(p.jar),
        "results": results,
    }

    print(json.dumps(report, indent=2))
    with open("nse_probe_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # ------------------------------------------------- job summary (markdown)
    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    if sm:
        eg = report["egress"]
        icon = {"VIABLE": "✅", "PARTIAL": "⚠️", "INCONCLUSIVE": "❔", "BLOCKED": "❌"}[verdict]
        lines = [
            f"## {icon} NSE reachability from GitHub Actions: **{verdict}**",
            "",
            summary,
            "",
            "### Egress (what NSE sees)",
            "",
            f"- IP: `{eg.get('ip', '?')}`",
            f"- Country: **{eg.get('country', '?')}**  "
            f"{'(NSE serves India traffic most permissively)' if eg.get('country') == 'IN' else '(NOT India)'}",
            f"- Network: {eg.get('asn_org') or eg.get('org') or '?'}",
            "",
            "### Endpoint results",
            "",
            "| Endpoint | Status | Outcome | ms | Bytes |",
            "|---|---|---|---|---|",
        ]
        for r in results:
            lines.append(
                f"| {r['label']} | {r.get('status') or '—'} | `{r['outcome']}` | "
                f"{r.get('elapsed_ms', '—')} | {r.get('bytes_read', 0)} |"
            )
        lines += [
            "",
            f"Cookies collected during warm-up: **{len(p.jar)}**",
            "",
            "> A `BLOCKED` or `not_published` result is a FINDING, not a CI failure — "
            "this job exits 0 either way so the answer is not mistaken for a broken build.",
        ]
        with open(sm, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    print(f"\n=== VERDICT: {verdict} ===\n{summary}")
    # Always 0. See the module docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main())
