#!/usr/bin/env bash
# Post-backfill finalization + verification, in dependency order.
#
# Run after `backfill` completes, or any time the parser / factor / universe
# logic changes. Every step is idempotent, so re-running is safe.
#
#   ./finalize.sh            # full chain
#   ./finalize.sh --no-net   # skip the external cross-check
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$HERE/.venv/bin/python"
CLI="$PY -m nse_eod.cli"
NONET=0
[ "${1:-}" = "--no-net" ] && NONET=1

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# 1. Calendar from evidence. MUST precede the audit: NSE's holiday API covers
#    only the current year, so without this a historical holiday is
#    indistinguishable from a missing ingest and the audit reports false gaps.
step "1/6 derive trading calendar from observed data"
$CLI derive-calendar

# 2. Re-parse every stored announcement with the CURRENT parser. A parser fix
#    that is not reparsed applies only to future ingests and leaves history wrong.
step "2/6 reparse corporate actions"
$CLI reparse-corp-actions

# 3. Resolve ISIN changes. MUST precede factors: a face-value split issues a new
#    ISIN and the CA feed often reports a superseded one, so without linkage the
#    event finds no bars and gets no anchor.
step "3/6 resolve ISIN changes"
$CLI link-isins

# 4. Re-derive every factor and rebuild the panel. --reset-factors is needed
#    whenever anchor or factor logic changed, because an event already in a
#    terminal state is otherwise never revisited.
step "4/6 recompute factors and materialize gold"
$CLI rebuild-adjusted --all --reset-factors

# 5. Internal audit: completeness and self-consistency.
step "5/6 audit panel"
$PY "$HERE/tools/audit_panel.py"
AUDIT=$?

# 6. Independent corroboration against a source with its own corporate-action
#    data. This is the only step that can confirm the factors are RIGHT rather
#    than merely self-consistent.
CROSS=0
if [ "$NONET" -eq 0 ]; then
  step "6/6 cross-check against Yahoo Finance"
  $PY "$HERE/tools/crosscheck_external.py" --n 60
  CROSS=$?
else
  step "6/6 cross-check SKIPPED (--no-net)"
fi

printf '\n\033[1m== summary ==\033[0m\n'
printf 'audit exit=%s  crosscheck exit=%s   (0 clean, 1 warnings, 2 problems)\n' "$AUDIT" "$CROSS"
[ "$AUDIT" -ge 2 ] || [ "$CROSS" -ge 2 ] && exit 2
[ "$AUDIT" -eq 1 ] || [ "$CROSS" -eq 1 ] && exit 1
exit 0
