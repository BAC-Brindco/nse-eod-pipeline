"""Detect which bhavcopy format a file is, from its header row.

NSE deprecated the legacy ``cm<DDMMMYYYY>bhav.csv`` in favour of the UDiFF
common bhavcopy. Measured coverage as at 2026-08-31:

    legacy : available up to ~2024-07, 404 from 2024-10 onward
    UDiFF  : available from 2024-01-02, 404 for 2023

Detection is by header content rather than by filename or date, so a file that
has been renamed, or served from an unexpected path, still parses correctly.
"""

from __future__ import annotations

UDIFF = "udiff"
LEGACY = "legacy"

# Columns unique to each format.
_UDIFF_MARKERS = {"TradDt", "FinInstrmTp", "TckrSymb", "SctySrs", "ClsPric"}
_LEGACY_MARKERS = {"SYMBOL", "SERIES", "PREVCLOSE", "TOTTRDQTY", "TIMESTAMP"}


class UnknownBhavFormat(ValueError):
    """The header matches neither known bhavcopy layout."""


def detect_format(header_line: str) -> str:
    """Return ``'udiff'`` or ``'legacy'`` for a bhavcopy header line."""
    cols = {c.strip().strip('"') for c in header_line.split(",")}
    if len(_UDIFF_MARKERS & cols) >= 4:
        return UDIFF
    upper = {c.upper() for c in cols}
    if len(_LEGACY_MARKERS & upper) >= 4:
        return LEGACY
    raise UnknownBhavFormat(
        f"unrecognised bhavcopy header: {sorted(cols)[:12]}"
    )


def detect_format_from_text(csv_text: str) -> str:
    first = csv_text.lstrip("﻿").splitlines()[0] if csv_text.strip() else ""
    if not first:
        raise UnknownBhavFormat("empty file")
    return detect_format(first)
