#!/usr/bin/env bash
# Local Postgres control for development.
#
# Portable EDB binaries under .tools/pgsql, data in .pgdata, port 5433 so it
# cannot collide with a system Postgres installed later on 5432.
#
#   ./pg.sh start | stop | status | createdb | psql | logs
#
# Moving to the server means changing DATABASE_URL in .env; this script is
# development-only scaffolding.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PGBIN="$HERE/.tools/pgsql/bin"
PGDATA="$HERE/.pgdata"
PORT=5433
USER=nse
DB=nse_eod
LOG="$HERE/logs/postgres.log"

export PGPASSWORD="${PGPASSWORD:-nsepipe_local_dev}"

if [ ! -x "$PGBIN/pg_ctl.exe" ] && [ ! -x "$PGBIN/pg_ctl" ]; then
  echo "Postgres binaries not found at $PGBIN" >&2
  echo "Download the EDB Windows zip and extract so that .tools/pgsql/bin exists." >&2
  exit 1
fi

PGCTL="$PGBIN/pg_ctl.exe"; [ -x "$PGCTL" ] || PGCTL="$PGBIN/pg_ctl"
PSQL="$PGBIN/psql.exe";    [ -x "$PSQL" ]  || PSQL="$PGBIN/psql"
CREATEDB="$PGBIN/createdb.exe"; [ -x "$CREATEDB" ] || CREATEDB="$PGBIN/createdb"

running() {
  # A plain `pg_ctl status` blocks on some Windows setups; probe the port instead.
  "$PSQL" -h 127.0.0.1 -p "$PORT" -U "$USER" -d postgres -c 'SELECT 1' >/dev/null 2>&1
}

case "${1:-status}" in
  start)
    if running; then echo "already running on port $PORT"; exit 0; fi
    mkdir -p "$HERE/logs"
    # pg_ctl start holds the terminal on Windows even when detached, so it is
    # backgrounded and the port is polled instead.
    "$PGCTL" -D "$PGDATA" -o "-p $PORT" -l "$LOG" start >/dev/null 2>&1 &
    for _ in $(seq 1 30); do
      if running; then echo "started on port $PORT"; exit 0; fi
      sleep 1
    done
    echo "failed to start; see $LOG" >&2
    tail -20 "$LOG" >&2 || true
    exit 1
    ;;
  stop)
    "$PGCTL" -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
    echo "stopped"
    ;;
  status)
    if running; then echo "running on port $PORT"; else echo "not running"; fi
    ;;
  createdb)
    "$CREATEDB" -h 127.0.0.1 -p "$PORT" -U "$USER" "$DB" 2>/dev/null \
      && echo "created $DB" || echo "$DB already exists"
    ;;
  psql)
    shift
    exec "$PSQL" -h 127.0.0.1 -p "$PORT" -U "$USER" -d "$DB" "$@"
    ;;
  logs)
    tail -n "${2:-40}" "$LOG"
    ;;
  *)
    echo "usage: $0 {start|stop|status|createdb|psql|logs}" >&2
    exit 2
    ;;
esac
