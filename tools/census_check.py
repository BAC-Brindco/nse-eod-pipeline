"""Run the corp-action parser over the full real census and report coverage.

Not a test: an operational audit. Run it after any parser change to see exactly
which real NSE strings would land in the review queue.

    .venv/Scripts/python.exe tools/census_check.py [--show-unknown N]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows consoles default to cp1252; keep output printable regardless.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nse_eod.parsers.corp_action_text import (  # noqa: E402
    NEEDS_EXTERNAL,
    NON_ADJUSTING,
    PRICE_ADJUSTING,
    parse_purpose,
    review_severity,
)

FIXTURE = ROOT / "tests" / "fixtures" / "corp_actions_real_2023_2026.json"


def tmpl(x: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+(\.\d+)?", "#", x)).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-unknown", type=int, default=25)
    ap.add_argument("--show-high", type=int, default=25)
    args = ap.parse_args()

    rows = [r for r in json.loads(FIXTURE.read_text(encoding="utf-8")) if isinstance(r, dict)]

    type_counts: collections.Counter[str] = collections.Counter()
    sev_counts: collections.Counter[str] = collections.Counter()
    unknown_templates: collections.Counter[str] = collections.Counter()
    high_templates: collections.Counter[str] = collections.Counter()
    rows_with_review = 0
    rows_price_adj = 0
    n_components = 0

    for r in rows:
        res = parse_purpose(r.get("subject", ""), r.get("faceVal"))
        n_components += len(res.components)
        row_flagged = False
        for c in res.components:
            type_counts[c.type] += 1
            if c.needs_review:
                sev = review_severity(c)
                sev_counts[sev] += 1
                row_flagged = True
                if c.type == "UNKNOWN":
                    unknown_templates[tmpl(c.raw_text)] += 1
                if sev == "high":
                    high_templates[tmpl(c.raw_text)] += 1
        if row_flagged:
            rows_with_review += 1
        if res.any_price_adjusting:
            rows_price_adj += 1

    print(f"census rows              : {len(rows)}")
    print(f"components extracted     : {n_components}  ({n_components/len(rows):.2f} per row)")
    print(f"rows w/ price adjustment : {rows_price_adj}")
    print(f"rows w/ >=1 review flag  : {rows_with_review}  ({rows_with_review/len(rows)*100:.1f}%)")
    print()
    print("--- review severity ---")
    for s in ("high", "medium", "low"):
        print(f"  {s:7s} {sev_counts.get(s,0)}")
    print()
    print("--- component types ---")
    for t, n in type_counts.most_common():
        bucket = (
            "PRICE-ADJ" if t in PRICE_ADJUSTING
            else "EXTERNAL" if t in NEEDS_EXTERNAL
            else "no-adjust" if t in NON_ADJUSTING
            else "div/other"
        )
        print(f"  {n:6d}  {t:24s} [{bucket}]")

    if unknown_templates:
        print(f"\n--- UNKNOWN templates (top {args.show_unknown}) ---")
        for t, n in unknown_templates.most_common(args.show_unknown):
            print(f"  {n:5d}  {t[:110]}")
    else:
        print("\n--- no UNKNOWN components ---")

    if high_templates:
        print(f"\n--- HIGH-severity review templates (top {args.show_high}) ---")
        for t, n in high_templates.most_common(args.show_high):
            print(f"  {n:5d}  {t[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
