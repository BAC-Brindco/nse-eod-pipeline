"""Cross-check our panel against an INDEPENDENT source (Yahoo Finance).

Yahoo maintains its own corporate-action database, so agreement is real evidence
rather than a self-consistency check.

**Matching the columns correctly is the whole game.** Yahoo's two price columns
mean specific things, and a naive comparison produces confident nonsense:

    Yahoo ``close``    = SPLIT-adjusted, NOT dividend-adjusted
    Yahoo ``adjclose`` = split-adjusted AND dividend-reinvested

    our ``c_raw``      = as-traded, never adjusted
    our ``c_adj``      = split/bonus/rights/special-cash adjusted, ordinary
                         dividends deliberately NOT reinvested
    our ``c_tr``       = c_adj plus ALL dividends reinvested

So the like-for-like pairs are:

  1. ``c_adj`` returns  <->  Yahoo ``close`` returns     (neither reinvests dividends)
       -> validates the split/bonus/rights factors.
  2. ``c_tr``  returns  <->  Yahoo ``adjclose`` returns  (both reinvest dividends)
       -> validates the total-return leg.

Comparing our ``c_raw`` LEVELS against Yahoo ``close`` levels was the original
mistake: it reports a "mismatch" for every stock that ever split, at exactly the
split ratio (360ONE 4.0x, AGIIL 10.0x, ANUHPHR 2.0x). That is a definitional
difference, not an error -- so raw levels are only compared over the window
AFTER the most recent split, where both series are on the same footing.

Levels are otherwise never compared: each provider anchors its adjustment at a
different date, so the series are proportional but not equal. Daily *returns*
are anchor-invariant, so if both apply the same actions the returns match to
rounding. A return mismatch on an ex-date is exactly the signature of a
disagreement about a corporate action.

    .venv/Scripts/python.exe tools/crosscheck_external.py                 # 40 symbols
    .venv/Scripts/python.exe tools/crosscheck_external.py --n 120 --ca-only
    .venv/Scripts/python.exe tools/crosscheck_external.py --symbol AJANTPHARM

Requires network. Exit 0 clean, 1 warnings, 2 mismatches that imply wrong prices.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows consoles default to cp1252; keep output printable regardless.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from nse_eod.db import fetch_all, fetch_one  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS"

# Tolerances. Raw prices should agree to a paisa or two; returns to a few bp.
RAW_TOL_PCT = 0.25       # % difference on close
RET_TOL = 0.005          # 50 bp on a daily return
MIN_OVERLAP = 30         # need this many shared bars to judge a symbol


def yahoo_daily(client: httpx.Client, symbol: str, lo: dt.date, hi: dt.date) -> dict | None:
    p1 = int(dt.datetime.combine(lo, dt.time()).timestamp())
    p2 = int(dt.datetime.combine(hi + dt.timedelta(days=2), dt.time()).timestamp())
    try:
        r = client.get(
            CHART.format(sym=symbol),
            params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,split"},
        )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        res = r.json()["chart"]["result"][0]
    except (KeyError, IndexError, TypeError, ValueError):
        return None

    ts = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    adj = (res.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    closes = quote.get("close")
    if not ts or not closes:
        return None

    out: dict[dt.date, tuple[float | None, float | None]] = {}
    for i, t in enumerate(ts):
        d = dt.datetime.fromtimestamp(t).date()
        c = closes[i] if i < len(closes) else None
        a = adj[i] if adj and i < len(adj) else None
        if c is not None:
            out[d] = (float(c), float(a) if a is not None else None)
    return out


def pick_symbols(n: int, ca_only: bool) -> list[dict]:
    """Prefer symbols WITH corporate actions -- that is where errors hide."""
    with_ca = fetch_all(
        """
        SELECT DISTINCT g.symbol, g.isin
          FROM gold.eod_adjusted g
          JOIN silver.corp_action_event e
            ON COALESCE(e.canonical_isin, e.isin) = g.isin
         WHERE g.in_universe
           AND g.series_flag = 'EQ'
           AND e.factor_price IS NOT NULL AND e.factor_price <> 1
           AND e.superseded_at IS NULL
         ORDER BY g.symbol
        """
    )
    # Deterministic spread across the alphabet rather than a random sample, so
    # repeated runs are comparable.
    step = max(1, len(with_ca) // max(n, 1))
    chosen = with_ca[::step][:n]
    if ca_only or len(chosen) >= n:
        return chosen

    liquid = fetch_all(
        """
        SELECT symbol, isin FROM (
            SELECT DISTINCT ON (isin) symbol, isin, adv_20
              FROM gold.eod_adjusted
             WHERE in_universe AND series_flag = 'EQ' AND adv_20 IS NOT NULL
             ORDER BY isin, trade_date DESC
        ) t ORDER BY adv_20 DESC NULLS LAST LIMIT %s
        """,
        (n * 3,),
    )
    seen = {c["isin"] for c in chosen}
    for row in liquid:
        if len(chosen) >= n:
            break
        if row["isin"] not in seen:
            chosen.append(row)
            seen.add(row["isin"])
    return chosen


def our_series(isin: str) -> list[dict]:
    return fetch_all(
        """
        SELECT trade_date, c_raw, c_adj, c_tr, cum_factor, cum_factor_tr
          FROM gold.eod_adjusted
         WHERE isin = %s AND series_flag = 'EQ'
           AND c_raw > 0 AND c_adj > 0
         ORDER BY trade_date
        """,
        (isin,),
    )


def _return_gaps(
    shared: list[dict], theirs: dict, ours_col: str, theirs_ix: int
) -> tuple[list[float], list[tuple]]:
    """Daily-return gaps between one of our columns and one of Yahoo's."""
    gaps: list[float] = []
    bad: list[tuple] = []
    for i in range(1, len(shared)):
        d0, d1 = shared[i - 1]["trade_date"], shared[i]["trade_date"]
        t0, t1 = theirs[d0][theirs_ix], theirs[d1][theirs_ix]
        o0, o1 = shared[i - 1][ours_col], shared[i][ours_col]
        if not (t0 and t1 and t0 > 0 and o0 and o1 and float(o0) > 0):
            continue
        our_ret = float(o1) / float(o0) - 1
        their_ret = float(t1) / float(t0) - 1
        gap = abs(our_ret - their_ret)
        gaps.append(gap)
        if gap > RET_TOL:
            bad.append((d1, our_ret, their_ret, gap))
    return gaps, bad


def compare(symbol: str, ours: list[dict], theirs: dict) -> dict:
    shared = [r for r in ours if r["trade_date"] in theirs]
    if len(shared) < MIN_OVERLAP:
        return {"symbol": symbol, "status": "skip", "reason": f"only {len(shared)} shared bars"}

    # ---- 1. raw LEVELS, only where both sides are on the same footing ------
    # Yahoo back-adjusts `close` for splits, and -- INCONSISTENTLY -- sometimes
    # for dividends too. SAREGAMA showed a constant 2.88% offset across 314 bars
    # (a dividend) while ABBOTINDIA's `close` was not dividend-adjusted. So raw
    # levels are only comparable after the last split AND the last dividend:
    # cum_factor == 1 marks the post-split window, cum_factor_tr == 1 the
    # post-dividend one. Requiring both is what makes this test trustworthy
    # rather than a source of false alarms.
    unsplit = [
        r
        for r in shared
        if abs(float(r["cum_factor"]) - 1.0) < 1e-9
        and abs(float(r["cum_factor_tr"] or 1.0) - 1.0) < 1e-9
    ]
    raw_diffs, raw_bad = [], []
    for r in unsplit:
        y_close = theirs[r["trade_date"]][0]
        ours_c = float(r["c_raw"])
        if y_close and y_close > 0:
            pct = abs(ours_c - y_close) / y_close * 100
            raw_diffs.append(pct)
            if pct > RAW_TOL_PCT:
                raw_bad.append((r["trade_date"], ours_c, y_close, pct))

    # ---- 2. price-adjusted RETURNS vs Yahoo `close` RETURNS ----------------
    # Yahoo `close` is split-adjusted but NOT dividend-reinvested, which is
    # exactly our c_adj convention. Tests the split/bonus/rights factors.
    adj_gaps, adj_bad = _return_gaps(shared, theirs, "c_adj", 0)

    # ---- 3. total-return RETURNS vs Yahoo `adjclose` RETURNS ---------------
    # Both reinvest every dividend. Tests the *_tr leg.
    tr_gaps, tr_bad = _return_gaps(shared, theirs, "c_tr", 1)

    status = "ok"
    if raw_bad and len(raw_bad) > max(2, 0.02 * max(len(unsplit), 1)):
        status = "RAW_MISMATCH"
    elif adj_bad and len(adj_bad) > max(2, 0.02 * max(len(adj_gaps), 1)):
        status = "ADJ_MISMATCH"
    elif tr_bad and len(tr_bad) > max(3, 0.03 * max(len(tr_gaps), 1)):
        status = "TR_MISMATCH"
    elif raw_bad or adj_bad or tr_bad:
        status = "minor"

    return {
        "symbol": symbol,
        "status": status,
        "bars": len(shared),
        "unsplit_bars": len(unsplit),
        "raw_median_pct": round(statistics.median(raw_diffs), 4) if raw_diffs else None,
        "raw_bad": raw_bad[:4],
        "raw_bad_n": len(raw_bad),
        "adj_median_bp": round(statistics.median(adj_gaps) * 10000, 2) if adj_gaps else None,
        "adj_bad": adj_bad[:4],
        "adj_bad_n": len(adj_bad),
        "tr_median_bp": round(statistics.median(tr_gaps) * 10000, 2) if tr_gaps else None,
        "tr_bad": tr_bad[:4],
        "tr_bad_n": len(tr_bad),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--ca-only", action="store_true", help="only symbols with corporate actions")
    ap.add_argument("--symbol", action="append", help="check specific symbol(s)")
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()

    span = fetch_one("SELECT min(trade_date) AS lo, max(trade_date) AS hi FROM gold.eod_adjusted")
    if not span or not span["lo"]:
        print("gold panel is empty")
        return 2
    lo, hi = span["lo"], span["hi"]

    if args.symbol:
        targets = []
        for sym in args.symbol:
            r = fetch_one(
                "SELECT DISTINCT symbol, isin FROM gold.eod_adjusted WHERE symbol = %s LIMIT 1",
                (sym.upper(),),
            )
            if r:
                targets.append(r)
            else:
                print(f"symbol not in panel: {sym}")
    else:
        targets = pick_symbols(args.n, args.ca_only)

    print(f"cross-checking {len(targets)} symbol(s) against Yahoo Finance over {lo} -> {hi}")
    print(f"tolerances: raw close {RAW_TOL_PCT}% | adjusted daily return {RET_TOL*10000:.0f} bp")
    print("-" * 100)

    results = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as client:
        for t in targets:
            y = yahoo_daily(client, t["symbol"], lo, hi)
            if not y:
                results.append({"symbol": t["symbol"], "status": "skip", "reason": "no Yahoo data"})
            else:
                results.append(compare(t["symbol"], our_series(t["isin"]), y))
            time.sleep(args.sleep)

    ok = [r for r in results if r["status"] == "ok"]
    minor = [r for r in results if r["status"] == "minor"]
    raw_bad = [r for r in results if r["status"] == "RAW_MISMATCH"]
    adj_bad = [r for r in results if r["status"] == "ADJ_MISMATCH"]
    tr_bad = [r for r in results if r["status"] == "TR_MISMATCH"]
    skipped = [r for r in results if r["status"] == "skip"]

    for r in results:
        if r["status"] == "skip":
            print(f"  skip    {r['symbol']:<14} {r.get('reason','')}")
            continue
        print(
            f"  {r['status']:<13} {r['symbol']:<13} {r['bars']:>5} bars "
            f"({r['unsplit_bars']} post-split)  "
            f"raw {r['raw_median_pct']}%  adj {r['adj_median_bp']}bp  tr {r['tr_median_bp']}bp  "
            f"(outliers raw {r['raw_bad_n']} / adj {r['adj_bad_n']} / tr {r['tr_bad_n']})"
        )
        for d, o, t_, pct in r["raw_bad"]:
            print(f"            raw  {d}  ours {o:.2f} vs yahoo {t_:.2f}  ({pct:.2f}%)")
        for d, o, t_, gap in r["adj_bad"]:
            print(
                f"            adj  {d}  ours {o*100:+.2f}% vs yahoo-close {t_*100:+.2f}%  "
                f"(gap {gap*10000:.0f}bp)"
            )
        for d, o, t_, gap in r["tr_bad"]:
            print(
                f"            tr   {d}  ours {o*100:+.2f}% vs yahoo-adj {t_*100:+.2f}%  "
                f"(gap {gap*10000:.0f}bp)"
            )

    print("-" * 100)
    print(
        f"clean {len(ok)} | minor {len(minor)} | RAW {len(raw_bad)} | "
        f"ADJ {len(adj_bad)} | TR {len(tr_bad)} | skipped {len(skipped)}"
    )
    if raw_bad:
        print("RAW mismatches mean our ingested prices disagree with an independent source.")
    if adj_bad:
        print("ADJ mismatches mean we and Yahoo disagree about a split/bonus/rights factor.")
    if tr_bad:
        print("TR mismatches mean we and Yahoo disagree about dividend reinvestment.")

    compared = len(ok) + len(minor) + len(raw_bad) + len(adj_bad) + len(tr_bad)
    if compared == 0:
        # Claiming corroboration when nothing was compared would be worse than
        # reporting nothing at all.
        print(
            f"RESULT: INCONCLUSIVE — 0 of {len(results)} symbols could be compared "
            f"(need >= {MIN_OVERLAP} shared bars each). Nothing was verified."
        )
        return 1
    if not raw_bad and not adj_bad:
        print(
            f"RESULT: prices and corporate-action adjustments corroborated independently "
            f"on {compared} symbol(s)."
        )
    return 2 if (raw_bad or adj_bad or tr_bad) else (1 if minor else 0)


if __name__ == "__main__":
    raise SystemExit(main())
