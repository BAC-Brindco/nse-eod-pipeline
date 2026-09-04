"""Refuse a DATABASE_URL that cannot work on a runner, and say exactly why.

WHY THIS EXISTS
---------------
A backfill run failed with:

    connection to server at "2406:da12:557:f800:34f4:c5dc:50ff:7d63", port 5432
    failed: Network is unreachable

That is Supabase's DIRECT host, which publishes only AAAA records on projects created
after early 2024. GitHub-hosted runners have no IPv6, so the address resolves and is
then permanently unreachable. The message names neither Supabase nor IPv6 nor the
fix, and "Network is unreachable" invites you to suspect an outage.

Three misconfigurations produce three different confusing errors, and this script
turns each into one sentence:

  1. direct host           -> "Network is unreachable" (IPv6, from a v4-only runner)
  2. pooler + wrong user   -> "(ENOTFOUND) tenant/user ... not found", which reads
                              like a permissions problem
  3. transaction pooler    -> NO ERROR AT ALL, and that is the dangerous one. The app
                              sets default_transaction_read_only and a statement
                              timeout, then commits; a transaction pooler releases the
                              server connection at that commit, so both settings
                              silently stop applying and the read-only guarantee is
                              gone with nothing to indicate it.

It never prints the DSN, only its shape.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

PROBLEMS: list[str] = []
NOTES: list[str] = []


def main() -> int:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        print("::error::DATABASE_URL is not set. Add it as a repository secret.")
        return 1

    try:
        u = urlsplit(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"::error::DATABASE_URL is not a parseable URL: {type(exc).__name__}")
        return 1

    host = (u.hostname or "").lower()
    port = u.port or 5432
    user = u.username or ""
    db = (u.path or "/").lstrip("/")

    # Shape only -- never the password, never the full string.
    print(f"  host : {host}")
    print(f"  port : {port}")
    print(f"  user : {user}")
    print(f"  db   : {db}")
    print()

    is_direct = host.startswith("db.") and host.endswith(".supabase.co")
    is_pooler = "pooler.supabase.com" in host

    if is_direct:
        ref = host.split(".")[1] if host.count(".") >= 2 else "<ref>"
        PROBLEMS.append(
            "This is Supabase's DIRECT host. It is IPv6-only on projects created "
            "after early 2024, and GitHub runners have no IPv6, so it can never "
            "connect from here. Use the SESSION POOLER instead -- and note that the "
            "USERNAME changes too, not just the host:\n"
            f"        postgresql://postgres.{ref}:<password>"
            f"@aws-<N>-<region>.pooler.supabase.com:5432/postgres\n"
            "      Copy it from Supabase -> Connect -> Session pooler rather than "
            "assembling it; the aws-N prefix and region differ per project."
        )

    if is_pooler and port == 6543:
        PROBLEMS.append(
            "Port 6543 is the TRANSACTION pooler. This application cannot use it: "
            "db.py sets `default_transaction_read_only = on` and a statement_timeout "
            "and then COMMITS, and a transaction pooler returns the server connection "
            "to the pool at that commit -- so both settings silently stop applying "
            "and the read-only guarantee on bronze/silver/gold is gone with nothing "
            "raised. psycopg3's automatic prepared statements break there too. "
            "Use port 5432, the session pooler."
        )

    if is_pooler and user == "postgres":
        PROBLEMS.append(
            "The pooler needs the project ref IN THE USERNAME -- "
            "`postgres.<project-ref>`, not plain `postgres`. With plain `postgres` "
            "the pooler answers '(ENOTFOUND) tenant/user postgres not found', which "
            "reads like a permissions problem and sends you looking in the wrong "
            "place."
        )

    if is_pooler and port == 5432 and user.startswith("postgres."):
        NOTES.append("session pooler, project ref in the username: correct shape")

    if not is_direct and not is_pooler:
        NOTES.append(
            "not a Supabase host -- shape checks skipped, connectivity is still "
            "verified below"
        )

    for p in PROBLEMS:
        print(f"::error::{p}")
    for n in NOTES:
        print(f"::notice::{n}")

    if PROBLEMS:
        return 1

    # Shape is fine; prove it actually connects, and that session state survives --
    # which is the property the transaction pooler quietly breaks.
    try:
        import psycopg
    except ImportError:
        print("::notice::psycopg not available; shape checked, connection not tested")
        return 0

    try:
        with psycopg.connect(raw, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 12345")
            conn.commit()
            # A session pooler keeps the same backend across that commit, so the SET
            # survives. A transaction pooler does not -- this is the actual test, not
            # a guess from the port number.
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout")
                got = cur.fetchone()[0]
                cur.execute("select current_user, current_database(), version()")
                who, dbname, ver = cur.fetchone()
        print(f"  connected as {who} to {dbname}")
        print(f"  {ver.split(' on ')[0]}")
        if got != "12345ms":
            print(
                "::error::A session-level SET did NOT survive a commit "
                f"(statement_timeout came back as {got!r}, expected '12345ms'). "
                "That is the signature of a TRANSACTION pooler, and it means "
                "`default_transaction_read_only = on` will not hold either -- the "
                "read-only guarantee would be silently absent. Switch to the session "
                "pooler on port 5432."
            )
            return 1
        print("::notice::session state survives a commit: session pooling confirmed")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0][:400]
        hint = ""
        if "Network is unreachable" in msg:
            hint = (
                " This is almost certainly the IPv6-only direct host; use the session "
                "pooler."
            )
        elif "not found" in msg.lower():
            hint = " Check the username carries the project ref: postgres.<ref>."
        elif "password authentication failed" in msg.lower():
            hint = (
                " Host and user are right; the PASSWORD is wrong. Reset it under "
                "Supabase -> Settings -> Database."
            )
        print(f"::error::Could not connect: {msg}{hint}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
