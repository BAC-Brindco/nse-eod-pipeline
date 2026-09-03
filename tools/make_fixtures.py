"""Download real NSE files and save trimmed test fixtures (network required).

    .venv/Scripts/python.exe tools/make_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)

H = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/all-reports",
}

ARCH = "https://nsearchives.nseindia.com"

TARGETS = [
    # (name, url, kind)
    ("udiff_20260828.csv", f"{ARCH}/content/cm/BhavCopy_NSE_CM_0_0_0_20260828_F_0000.csv.zip", "zip"),
    ("delivery_28082026.csv", f"{ARCH}/products/content/sec_bhavdata_full_28082026.csv", "csv"),
    ("pricebands_28082026.csv", f"{ARCH}/content/equities/sec_list_28082026.csv", "csv"),
    ("equity_l.csv", f"{ARCH}/content/equities/EQUITY_L.csv", "csv"),
]

# Overlap window: both formats exist for 2024-07-01, so the two parsers can be
# cross-checked against each other on identical data. These two fixtures must
# cover the SAME securities -- the formats sort differently, so a naive
# head-400 of each would barely intersect and the cross-check would be vacuous.
OVERLAP = {
    "udiff_20240701.csv": (
        f"{ARCH}/content/cm/BhavCopy_NSE_CM_0_0_0_20240701_F_0000.csv.zip",
        7,   # UDiFF TckrSymb column index
        8,   # UDiFF SctySrs column index
    ),
    "legacy_01JUL2024.csv": (
        f"{ARCH}/content/historical/EQUITIES/2024/JUL/cm01JUL2024bhav.csv.zip",
        0,   # legacy SYMBOL
        1,   # legacy SERIES
    ),
}

MAX_ROWS = 400  # keep fixtures small; header + first N data rows


def main() -> int:
    from nse_eod.parsers.bhav import unzip_single_csv

    with httpx.Client(timeout=60, headers=H, follow_redirects=True) as c:
        for name, url, kind in TARGETS:
            try:
                r = c.get(url)
            except httpx.HTTPError as exc:
                print(f"FAIL   {name}: {exc}")
                continue
            if r.status_code != 200:
                print(f"FAIL   {name}: HTTP {r.status_code}")
                continue
            text = unzip_single_csv(r.content) if kind == "zip" else r.text
            lines = text.splitlines()
            trimmed = "\n".join(lines[: MAX_ROWS + 1]) + "\n"
            (FIX / name).write_text(trimmed, encoding="utf-8")
            print(f"OK     {name}: {len(lines)} rows -> kept {min(len(lines)-1, MAX_ROWS)}")

        # ---- overlap pair, aligned on a shared (symbol, series) set ----------
        fetched: dict[str, tuple[list[str], int, int]] = {}
        for name, (url, sym_ix, ser_ix) in OVERLAP.items():
            r = c.get(url)
            if r.status_code != 200:
                print(f"FAIL   {name}: HTTP {r.status_code}")
                continue
            fetched[name] = (unzip_single_csv(r.content).splitlines(), sym_ix, ser_ix)

        if len(fetched) == len(OVERLAP):
            keysets = []
            for lines, si, sj in fetched.values():
                keys = set()
                for ln in lines[1:]:
                    f = ln.split(",")
                    if len(f) > max(si, sj):
                        keys.add((f[si].strip().upper(), f[sj].strip().upper()))
                keysets.append(keys)
            common = sorted(set.intersection(*keysets))[:MAX_ROWS]
            common_set = set(common)
            print(f"       overlap: {len(common_set)} shared (symbol, series) keys")
            for name, (lines, si, sj) in fetched.items():
                out = [lines[0]]
                for ln in lines[1:]:
                    f = ln.split(",")
                    if len(f) > max(si, sj) and (
                        f[si].strip().upper(),
                        f[sj].strip().upper(),
                    ) in common_set:
                        out.append(ln)
                (FIX / name).write_text("\n".join(out) + "\n", encoding="utf-8")
                print(f"OK     {name}: aligned -> {len(out)-1} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
