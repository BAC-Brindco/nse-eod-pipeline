"""Record a probe verdict in ops.egress_probe so it is readable without repo access.

WHY THIS IS A SEPARATE SCRIPT FROM THE PROBE
--------------------------------------------
probe_nse_egress.py is deliberately stdlib-only: a dependency install is another
thing that can fail, and a probe whose own setup breaks tells you nothing about NSE.
That property is worth keeping, so the probe writes nse_probe_report.json and this
script -- which does need psycopg -- reads the file afterwards. If the install or the
database is unavailable, the probe's own result is already safely on disk and in the
job summary.

NEVER FAILS THE JOB
-------------------
Exits 0 on every path. The probe's job is to report on NSE; a bookkeeping failure
must not turn a successful measurement into a red build, because the next person to
see red will assume NSE is down.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPORT = Path("nse_probe_report.json")


def main() -> int:
    if not REPORT.exists():
        print(f"::warning::{REPORT} not found; nothing to record")
        return 0

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        # Normal on a repo where the secret has not been set. Say so plainly rather
        # than looking like a failure.
        print("::notice::DATABASE_URL is not set, so the verdict was not recorded. "
              "The job summary and the uploaded artifact still carry it.")
        return 0

    try:
        report = json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::could not parse {REPORT}: {exc!r}")
        return 0

    results = report.get("results", [])
    arch = [r for r in results if "nsearchives" in r.get("url", "")]
    arch_ok = [r for r in arch if r.get("status") == 200]
    warm = next(
        (r for r in results if "WARM" in (r.get("label") or "")), {}
    )
    eg = report.get("egress", {}) or {}

    row = {
        "verdict": report.get("verdict", "ERROR"),
        "summary": report.get("summary"),
        "egress_ip": eg.get("ip"),
        "egress_country": eg.get("country"),
        "egress_network": eg.get("asn_org") or eg.get("org"),
        "runner_os": (report.get("runner") or {}).get("os"),
        "archives_ok": len(arch_ok),
        "archives_total": len(arch),
        "api_warm_ok": warm.get("status") == 200,
        "cookies": report.get("cookies_collected"),
        "report": json.dumps(report),
    }

    try:
        import psycopg
    except ImportError:
        print("::warning::psycopg not installed; verdict not recorded")
        return 0

    try:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.egress_probe
                    (verdict, summary, egress_ip, egress_country, egress_network,
                     runner_os, archives_ok, archives_total, api_warm_ok, cookies,
                     report)
                VALUES (%(verdict)s, %(summary)s, %(egress_ip)s, %(egress_country)s,
                        %(egress_network)s, %(runner_os)s, %(archives_ok)s,
                        %(archives_total)s, %(api_warm_ok)s, %(cookies)s,
                        %(report)s::jsonb)
                RETURNING id
                """,
                row,
            )
            new_id = cur.fetchone()[0]
        print(
            f"::notice::recorded probe #{new_id}: {row['verdict']} from "
            f"{row['egress_country']} ({row['egress_network']}), "
            f"{row['archives_ok']}/{row['archives_total']} archives, "
            f"warm API {'OK' if row['api_warm_ok'] else 'no'}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::could not record the verdict: {exc!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
