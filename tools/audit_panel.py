"""Full-history audit of the adjusted panel.

Answers two questions with evidence, not assertion:
    1. Is anything MISSING?   (trading days, symbols, per-symbol gaps)
    2. Are the prices CORRECT? (OHLC sanity, prev_close chain, factor coverage,
                                unexplained jumps, right-edge identity)

Run after a backfill and before trusting the panel:

    .venv/Scripts/python.exe tools/audit_panel.py
    .venv/Scripts/python.exe tools/audit_panel.py --json   # machine-readable

Exit code 0 = clean, 1 = warnings, 2 = problems that affect prices.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows consoles default to cp1252; keep output printable regardless.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nse_eod.db import fetch_all, fetch_one  # noqa: E402

OK, WARN, BAD = "OK", "WARN", "PROBLEM"


class Audit:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, name: str, status: str, detail: str, data=None) -> None:
        self.results.append({"check": name, "status": status, "detail": detail, "data": data})

    @property
    def exit_code(self) -> int:
        if any(r["status"] == BAD for r in self.results):
            return 2
        if any(r["status"] == WARN for r in self.results):
            return 1
        return 0

    def render(self) -> None:
        icon = {OK: "  ok   ", WARN: " WARN  ", BAD: "PROBLEM"}
        for r in self.results:
            print(f"[{icon[r['status']]}] {r['check']:<34} {r['detail']}")
            if r["data"]:
                for line in r["data"][:8]:
                    print(f"                {line}")
                if len(r["data"]) > 8:
                    print(f"                ... and {len(r['data'])-8} more")


# ---------------------------------------------------------------- 1. coverage
def audit_coverage(a: Audit) -> None:
    r = fetch_one(
        """
        SELECT count(*) AS bars, count(DISTINCT isin) AS isins,
               count(DISTINCT symbol) AS symbols, count(DISTINCT trade_date) AS days,
               min(trade_date) AS first_day, max(trade_date) AS last_day
          FROM bronze.eod_bhav_raw
        """
    )
    a.add(
        "bronze.coverage",
        OK if r["bars"] else BAD,
        f"{r['bars']:,} bars | {r['isins']:,} ISINs | {r['symbols']:,} symbols | "
        f"{r['days']:,} days | {r['first_day']} -> {r['last_day']}",
    )

    g = fetch_one(
        """
        SELECT count(*) AS rows, count(DISTINCT isin) AS isins,
               count(DISTINCT trade_date) AS days,
               count(*) FILTER (WHERE in_universe) AS universe_rows,
               count(DISTINCT isin) FILTER (WHERE in_universe) AS universe_isins
          FROM gold.eod_adjusted
        """
    )
    a.add(
        "gold.coverage",
        OK if g["rows"] else BAD,
        f"{g['rows']:,} rows | {g['isins']:,} ISINs | {g['days']:,} days | "
        f"universe {g['universe_rows']:,} rows / {g['universe_isins']:,} stocks",
    )

    # Universe composition, so the SME inclusion is visible and checkable.
    comp = fetch_all(
        """
        SELECT series_flag, count(DISTINCT isin) AS isins, count(*) AS rows,
               count(*) FILTER (WHERE in_universe) AS in_univ
          FROM gold.eod_adjusted GROUP BY series_flag ORDER BY 2 DESC
        """
    )
    a.add(
        "gold.universe_composition",
        OK,
        "series breakdown:",
        [
            f"{c['series_flag']:<5} {c['isins']:>5} isins  {c['rows']:>9,} rows  "
            f"{'IN universe' if c['in_univ'] else 'flagged out'}"
            for c in comp
        ],
    )


# ------------------------------------------------------- 2. missing trading days
def audit_missing_days(a: Audit) -> None:
    """Weekdays inside the ingested range with no bars and no holiday reason."""
    rows = fetch_all(
        """
        WITH span AS (
            SELECT min(trade_date) AS lo, max(trade_date) AS hi FROM bronze.eod_bhav_raw
        ),
        cal AS (
            SELECT d::date AS cal_date
              FROM span, generate_series(span.lo, span.hi, INTERVAL '1 day') d
             WHERE extract(isodow FROM d) < 6
        )
        SELECT c.cal_date, tc.is_trading_day, tc.reason, tc.source
          FROM cal c
          LEFT JOIN bronze.trading_calendar tc ON tc.cal_date = c.cal_date
         WHERE NOT EXISTS (
                SELECT 1 FROM bronze.eod_bhav_raw b WHERE b.trade_date = c.cal_date
               )
         ORDER BY c.cal_date
        """
    )
    unexplained = [
        r for r in rows if r["is_trading_day"] is None or r["is_trading_day"] is True
    ]
    holidays = len(rows) - len(unexplained)

    # Weekend sessions are REAL and this check only looks at weekdays, so report
    # what was captured. NSE held 6 Saturday and 2 Sunday sessions in
    # 2020-2026 (Budget days, Diwali Muhurat, special live sessions); a gap here
    # would be invisible to the weekday scan above, which is exactly how the
    # Sunday sessions stayed hidden until a prev_close mismatch exposed them.
    weekend = fetch_all(
        """
        SELECT trade_date, to_char(trade_date, 'Dy') AS dow, count(*) AS bars
          FROM bronze.eod_bhav_raw
         WHERE extract(isodow FROM trade_date) >= 6
         GROUP BY 1, 2 ORDER BY 1
        """
    )
    a.add(
        "weekend_sessions",
        OK,
        f"{len(weekend)} weekend session(s) captured "
        f"({sum(1 for w in weekend if w['dow'].strip() == 'Sat')} Sat, "
        f"{sum(1 for w in weekend if w['dow'].strip() == 'Sun')} Sun) — "
        "every weekend day is probed; only a 404 rules one out",
        [f"{w['trade_date']} {w['dow']}  {w['bars']:,} bars" for w in weekend],
    )

    if not unexplained:
        a.add(
            "missing_trading_days",
            OK,
            f"no gaps: every weekday in range has bars or a holiday reason ({holidays} holidays)",
        )
    else:
        a.add(
            "missing_trading_days",
            BAD,
            f"{len(unexplained)} weekday(s) have NO bars and are not marked holidays "
            f"({holidays} explained holidays)",
            [f"{r['cal_date']}  calendar={r['reason'] or 'unknown'}" for r in unexplained],
        )


# ------------------------------------------------------- 3. per-day row counts
def audit_daily_rowcounts(a: Audit) -> None:
    rows = fetch_all(
        """
        WITH per_day AS (
            SELECT trade_date, count(*)::numeric AS n FROM bronze.eod_bhav_raw
             GROUP BY trade_date
        ),
        stats AS (SELECT avg(n) AS mean, stddev_samp(n) AS sd FROM per_day)
        SELECT p.trade_date, p.n::int AS n,
               round(((p.n - s.mean) / NULLIF(s.sd, 0))::numeric, 1) AS z
          FROM per_day p, stats s
         WHERE s.sd > 0 AND abs((p.n - s.mean) / s.sd) > 5
         ORDER BY abs((p.n - s.mean) / s.sd) DESC
         LIMIT 20
        """
    )
    span = fetch_one(
        """
        SELECT min(n) AS lo, max(n) AS hi, round(avg(n)) AS avg FROM (
            SELECT count(*) AS n FROM bronze.eod_bhav_raw GROUP BY trade_date
        ) t
        """
    )
    detail = f"per-day rows: min {span['lo']:,} | avg {int(span['avg']):,} | max {span['hi']:,}"
    if not rows:
        a.add("daily_rowcount_outliers", OK, detail + " — no >5sigma outliers")
    else:
        a.add(
            "daily_rowcount_outliers",
            WARN,
            detail + f" — {len(rows)} day(s) beyond 5sigma (often a genuine market-wide event)",
            [f"{r['trade_date']}  {r['n']:,} rows  ({r['z']:+}sigma)" for r in rows],
        )


# ------------------------------------------------------------ 4. price sanity
# Series that actually reach the gold panel. The OHLC-range check is scoped to
# these because NSE's T0 (same-day settlement) pilot, launched 2024-03-28,
# publishes a computed close that sits OUTSIDE the traded range on near-zero
# volume (1-6 trades). All 216 range violations in the first full-history run
# were T0; zero were EQ, and gold had zero. It is a characteristic of a pilot
# segment that never enters the panel, not a defect in our ingest.
_GOLD_SERIES_SQL = "('EQ','BE','BZ','SM','ST')"


def audit_price_sanity(a: Audit) -> None:
    r = fetch_one(
        f"""
        SELECT
          count(*) FILTER (WHERE c IS NULL OR c <= 0)                  AS bad_close,
          count(*) FILTER (WHERE h IS NOT NULL AND l IS NOT NULL AND l > h) AS lo_gt_hi,
          count(*) FILTER (WHERE h IS NOT NULL AND c > h + 0.0001)     AS close_above_high,
          count(*) FILTER (WHERE l IS NOT NULL AND c < l - 0.0001)     AS close_below_low,
          count(*) FILTER (WHERE o IS NOT NULL AND h IS NOT NULL AND o > h + 0.0001) AS open_above_high,
          count(*) FILTER (WHERE volume IS NOT NULL AND volume < 0)    AS neg_volume,
          count(*) FILTER (WHERE turnover IS NOT NULL AND turnover < 0) AS neg_turnover
          FROM bronze.eod_bhav_raw
         WHERE series IN {_GOLD_SERIES_SQL}
        """
    )
    problems = {k: v for k, v in r.items() if v}
    if not problems:
        a.add(
            "bronze.price_sanity",
            OK,
            "OHLC ordering, signs and nulls all clean across panel-bound series",
        )
    else:
        a.add(
            "bronze.price_sanity",
            BAD,
            "raw price integrity violations in panel-bound series",
            [f"{k}: {v:,}" for k, v in problems.items()],
        )

    # Report the excluded segments separately so their quirks stay visible
    # rather than silently filtered away.
    other = fetch_all(
        f"""
        SELECT series, count(*) AS n,
               count(*) FILTER (WHERE h IS NOT NULL
                                  AND (c > h + 0.0001 OR c < l - 0.0001)) AS out_of_range
          FROM bronze.eod_bhav_raw
         WHERE series NOT IN {_GOLD_SERIES_SQL}
         GROUP BY series HAVING count(*) FILTER (WHERE h IS NOT NULL
                                  AND (c > h + 0.0001 OR c < l - 0.0001)) > 0
         ORDER BY 3 DESC
        """
    )
    if other:
        a.add(
            "non_panel_series_quirks",
            OK,
            "close outside [low,high] in series EXCLUDED from gold (informational):",
            [
                f"{o['series']:<4} {o['n']:>7,} bars  {o['out_of_range']:>4} out-of-range"
                + ("   <- NSE T+0 pilot: computed close on 1-6 trades" if o["series"] == "T0" else "")
                for o in other
            ],
        )

    g = fetch_one(
        """
        SELECT
          count(*) FILTER (WHERE c_adj IS NULL OR c_adj <= 0)      AS bad_c_adj,
          count(*) FILTER (WHERE cum_factor IS NULL OR cum_factor <= 0) AS bad_factor,
          count(*) FILTER (WHERE l_adj > h_adj)                    AS lo_gt_hi,
          count(*) FILTER (WHERE c_tr IS NOT NULL AND c_tr <= 0)   AS bad_c_tr,
          count(*) FILTER (WHERE v_adj IS NOT NULL AND v_adj < 0)  AS neg_v_adj
          FROM gold.eod_adjusted
        """
    )
    gp = {k: v for k, v in g.items() if v}
    if not gp:
        a.add("gold.price_sanity", OK, "adjusted OHLC, factors and TR series all positive & ordered")
    else:
        a.add("gold.price_sanity", BAD, "adjusted price violations", [f"{k}: {v:,}" for k, v in gp.items()])


# --------------------------------------------------- 5. prev_close chain check
def audit_prev_close_chain(a: Audit) -> None:
    """NSE's prev_close must equal our prior stored close (it is NOT CA-adjusted).

    Run across ALL history, matched on the canonical ISIN so a face-value split
    does not look like a break. A mismatch means a missing bar, a restatement, or
    a bad ISIN link -- each of which corrupts returns.
    """
    rows = fetch_all(
        """
        WITH canon AS (
            SELECT b.trade_date, b.series, b.symbol, b.c, b.prev_close,
                   COALESCE(l.canonical_isin, b.isin) AS isin
              FROM bronze.eod_bhav_raw b
              LEFT JOIN silver.isin_link l ON l.isin = b.isin
        ),
        chained AS (
            SELECT isin, symbol, series, trade_date, prev_close,
                   lag(c)          OVER w AS prior_c,
                   lag(trade_date) OVER w AS prior_d
              FROM canon
            WINDOW w AS (PARTITION BY isin, series ORDER BY trade_date)
        )
        SELECT symbol, isin, series, trade_date, prior_d, prior_c, prev_close,
               round((100*abs(prev_close-prior_c)/prior_c)::numeric, 3) AS pct
          FROM chained
         WHERE prior_c IS NOT NULL AND prior_c > 0
           AND prev_close IS NOT NULL AND prev_close > 0
           -- only adjacent bars; a long gap legitimately breaks the chain
           AND prior_d >= trade_date - INTERVAL '7 days'
           AND abs(prev_close - prior_c) / prior_c > 0.005
         ORDER BY abs(prev_close - prior_c) / prior_c DESC
         LIMIT 50
        """
    )
    total = fetch_one("SELECT count(*) AS n FROM bronze.eod_bhav_raw")["n"]
    if not rows:
        a.add(
            "prev_close_chain",
            OK,
            f"all {total:,} bars reconcile with NSE's prev_close (>0.5% tolerance)",
        )
    else:
        pct = len(rows) / max(total, 1) * 100
        a.add(
            "prev_close_chain",
            BAD if len(rows) > 50 else WARN,
            f"{len(rows)} bar(s) break the prev_close chain ({pct:.4f}% of bars)",
            [
                f"{r['symbol']:<12} {r['series']} {r['trade_date']}  "
                f"prior({r['prior_d']}) {float(r['prior_c']):.2f} vs prev_close "
                f"{float(r['prev_close']):.2f}  ({r['pct']}%)"
                for r in rows
            ],
        )


# ------------------------------------------------- 6. corp-action factor coverage
def audit_factor_coverage(a: Audit) -> None:
    r = fetch_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE factor_state = 'computed')       AS computed,
               count(*) FILTER (WHERE factor_state = 'not_applicable') AS na,
               count(*) FILTER (WHERE factor_state = 'skipped_s_gt_p') AS skipped,
               count(*) FILTER (WHERE factor_state IN ('pending','needs_anchor','unresolved')) AS open
          FROM silver.corp_action_event
         WHERE superseded_at IS NULL
        """
    )
    a.add(
        "corp_action_events",
        OK,
        f"{r['total']:,} events | {r['computed']:,} computed | {r['na']:,} not-applicable | "
        f"{r['skipped']:,} S>P skipped | {r['open']:,} still open",
    )

    # The one that matters: a price-adjusting event with an ANCHOR AVAILABLE but
    # no factor means the panel is silently wrong for that symbol.
    rows = fetch_all(
        """
        SELECT e.symbol, e.isin, e.ex_date, e.type, e.factor_state, e.raw_purpose
          FROM silver.corp_action_event e
         WHERE e.superseded_at IS NULL
           AND e.ex_date <= (SELECT max(trade_date) FROM bronze.eod_bhav_raw)
           AND e.factor_state IN ('pending','needs_anchor','unresolved')
           AND e.type IN ('BONUS','SPLIT','FACE_VALUE_CHANGE','RIGHTS',
                          'SPECIAL_DIVIDEND','RETURN_OF_CAPITAL')
           AND EXISTS (
                 SELECT 1 FROM silver.isin_link l
                   JOIN bronze.eod_bhav_raw b ON b.isin = l.isin
                  WHERE l.canonical_isin = COALESCE(e.canonical_isin, e.isin)
                    AND b.trade_date < e.ex_date AND b.c > 0
               )
         ORDER BY e.ex_date DESC
         LIMIT 50
        """
    )
    if not rows:
        a.add(
            "factors_missing_with_anchor",
            OK,
            "every anchorable price-adjusting event has a factor",
        )
    else:
        # Distinguish TRACKED from SILENT. An event sitting in the review queue
        # is a known, visible gap awaiting a human decision; one with no queue
        # entry is a hole nobody knows about. Both leave the symbol's history
        # unadjusted, but only the second is a defect in the pipeline.
        tracked = fetch_all(
            """
            SELECT DISTINCT q.isin, q.ex_date
              FROM silver.corp_action_review_queue q
             WHERE q.status = 'open' AND q.severity = 'high'
            """
        )
        known = {(t["isin"], t["ex_date"]) for t in tracked}
        silent = [r for r in rows if (r["isin"], r["ex_date"]) not in known]
        a.add(
            "factors_missing_with_anchor",
            BAD if silent else WARN,
            (
                f"{len(rows)} price-adjusting event(s) have bars before ex-date but no factor; "
                f"{len(rows)-len(silent)} are TRACKED in the high-severity review queue, "
                f"{len(silent)} are UNTRACKED"
                + (" — those are silent holes" if silent else " — none silent")
                + ". Affected symbols are unadjusted for that event until resolved via "
                "silver.corp_action_override."
            ),
            [
                f"{'SILENT ' if (r['isin'], r['ex_date']) not in known else 'tracked'} "
                f"{r['symbol']:<12} {r['ex_date']} {r['type']:<10} {r['factor_state']:<11} "
                f"{(r['raw_purpose'] or '')[:44]}"
                for r in rows
            ],
        )

    # Events outside the ingested window: expected, informational only.
    out = fetch_one(
        """
        SELECT count(*) AS n FROM silver.corp_action_event e
         WHERE e.superseded_at IS NULL
           AND e.factor_state IN ('pending','needs_anchor','unresolved')
           AND e.type IN ('BONUS','SPLIT','FACE_VALUE_CHANGE','RIGHTS',
                          'SPECIAL_DIVIDEND','RETURN_OF_CAPITAL')
           AND NOT EXISTS (
                 SELECT 1 FROM silver.isin_link l
                   JOIN bronze.eod_bhav_raw b ON b.isin = l.isin
                  WHERE l.canonical_isin = COALESCE(e.canonical_isin, e.isin)
                    AND b.trade_date < e.ex_date AND b.c > 0
               )
        """
    )
    if out["n"]:
        a.add(
            "factors_outside_window",
            OK,
            f"{out['n']:,} event(s) predate the first ingested bar (not anchorable, expected)",
        )


# ------------------------------------------- 6b. orphaned factors (silent killer)
def audit_orphaned_factors(a: Audit) -> None:
    """Computed factors that reach NO bars in the panel.

    The most dangerous silent failure in the whole design: the event parsed, the
    factor computed, ``factor_state = 'computed'`` -- everything looks healthy --
    but the ISIN it is keyed on has no bars, so the factor multiplies nothing and
    the symbol's history stays unadjusted.

    Found in the wild: HDFC Bank's 2025 Bonus 1:1 was filed by NSE under
    INE040A01018 (the pre-merger ISIN) while every bar sits under INE040A01034.
    The factor was 0.5 and correct; it simply never touched a row. India's
    largest private bank had its entire pre-August-2025 history unadjusted by 2x
    and no other check noticed, because every individual component was fine.
    """
    # Three distinct situations look identical in a naive query, and only the
    # first is a defect:
    #   a) the ISIN is WRONG      -- the panel HAS earlier bars for this security
    #                                under a different ISIN. HDFC Bank's case.
    #   b) history starts later   -- the event predates the security's first bar
    #                                (ALCODIS bonus 2024-10, first bar 2025-11).
    #                                Nothing to adjust; correct.
    #   c) not in the panel at all -- InvIT/REIT units (ISIN type 23/25), which
    #                                are excluded from gold by design.
    rows = fetch_all(
        """
        WITH candidates AS (
            SELECT e.symbol, e.isin, e.canonical_isin, e.ex_date, e.type,
                   e.factor_price, e.isin_resolved_via,
                   EXISTS (SELECT 1 FROM gold.eod_adjusted g
                            WHERE g.symbol = e.symbol) AS symbol_in_panel,
                   (SELECT min(g.trade_date) FROM gold.eod_adjusted g
                     WHERE g.symbol = e.symbol) AS symbol_first_bar
              FROM silver.corp_action_event e
             WHERE e.superseded_at IS NULL
               AND e.factor_price IS NOT NULL
               AND e.factor_price <> 1
               AND e.factor_state = 'computed'
               AND NOT EXISTS (
                     SELECT 1 FROM gold.eod_adjusted g
                      WHERE g.isin = COALESCE(e.canonical_isin, e.isin)
                        AND g.trade_date < e.ex_date
                   )
        )
        SELECT * FROM candidates
         WHERE symbol_in_panel
           -- (b): the security simply had no bars yet
           AND symbol_first_bar < ex_date
         ORDER BY ex_date DESC
         LIMIT 40
        """
    )
    benign = fetch_one(
        """
        SELECT count(*) AS n FROM silver.corp_action_event e
         WHERE e.superseded_at IS NULL
           AND e.factor_price IS NOT NULL AND e.factor_price <> 1
           AND e.factor_state = 'computed'
           AND NOT EXISTS (
                 SELECT 1 FROM gold.eod_adjusted g
                  WHERE g.isin = COALESCE(e.canonical_isin, e.isin)
                    AND g.trade_date < e.ex_date
               )
        """
    )["n"]
    if not rows:
        a.add(
            "orphaned_factors_benign",
            OK,
            f"{benign} computed factor(s) reach no bars, all explained: the event "
            "predates the security's first bar, or the instrument (InvIT/REIT unit) "
            "is excluded from the panel by design",
        )
    if not rows:
        a.add(
            "orphaned_factors",
            OK,
            "every computed price factor reaches bars in the panel",
        )
        return
    a.add(
        "orphaned_factors",
        BAD,
        f"{len(rows)} computed factor(s) reach NO bars — the event's ISIN has no "
        "history in the panel, so the adjustment silently does nothing",
        [
            f"{(r['symbol'] or '?'):<12} {r['ex_date']} {r['type']:<8} "
            f"factor={float(r['factor_price']):.6f}  event_isin={r['isin']}  "
            f"resolved={r['canonical_isin'] or 'NONE'}"
            for r in rows
        ],
    )


# ------------------------------------------------------- 7. unexplained jumps
def audit_unexplained_jumps(a: Audit) -> None:
    """>25% adjusted moves with no corporate action nearby, on non-penny stocks.

    The classic symptom of an unapplied action. Two calibrations matter:

    * 25% rather than 20%, so genuine single-day moves (results, news) do not
      dominate -- they exist in real markets.
    * a Rs 5 price FLOOR. Below that the minimum tick dominates the percentage:
      KSERASERA trades at Rs 0.10-0.15, so a single 5-paisa tick is +50% or
      +100%. All 40 findings in the first full-history run were sub-rupee stocks
      ticking by one paisa -- no corporate action involved.
    """
    rows = fetch_all(
        """
        WITH chained AS (
            SELECT isin, symbol, series, trade_date, c_adj,
                   lag(c_adj)      OVER w AS prior_c,
                   lag(trade_date) OVER w AS prior_d
              FROM gold.eod_adjusted
             WHERE in_universe AND c_adj > 5.0
            WINDOW w AS (PARTITION BY isin, series ORDER BY trade_date)
        )
        SELECT c.symbol, c.isin, c.trade_date, c.prior_d,
               round((100*(c.c_adj/c.prior_c - 1))::numeric, 1) AS move_pct
          FROM chained c
         WHERE c.prior_c IS NOT NULL AND c.prior_c > 0
           AND c.prior_d >= c.trade_date - INTERVAL '7 days'
           AND abs(c.c_adj/c.prior_c - 1) > 0.25
           AND NOT EXISTS (
                 SELECT 1 FROM silver.corp_action_event e
                  WHERE COALESCE(e.canonical_isin, e.isin) = c.isin
                    AND e.ex_date BETWEEN c.prior_d - 3 AND c.trade_date + 3
                    AND e.superseded_at IS NULL
               )
           -- A derived ISIN-switch already queued for review IS an explanation:
           -- the split is known and awaiting a human ratio, not undetected.
           -- JSLL and GICL both switched ISIN with prev_close continuity intact
           -- but have no corporate action in either NSE feed.
           AND NOT EXISTS (
                 SELECT 1 FROM silver.corp_action_review_queue q
                  WHERE q.status = 'open'
                    AND q.symbol = c.symbol
                    AND q.ex_date BETWEEN c.prior_d - 3 AND c.trade_date + 3
               )
         ORDER BY abs(c.c_adj/c.prior_c - 1) DESC
         LIMIT 40
        """
    )
    total = fetch_one(
        "SELECT count(*) AS n FROM gold.eod_adjusted WHERE in_universe AND c_adj > 5.0"
    )["n"]
    if not rows:
        a.add(
            "unexplained_jumps",
            OK,
            "no >25% adjusted move above Rs 5 lacks a corporate action",
        )
        return
    severe = [r for r in rows if abs(float(r["move_pct"])) > 45]
    # A >45% single-day move CANNOT be distinguished from a missed corporate
    # action by data alone -- real ones happen. Verified genuine in this panel:
    # YESBANK 2020-03-06 (-56%, RBI moratorium), 2020-03-16/17 (+45%/+58%, SBI
    # rescue) and INDUSINDBK 2020-03-26 (+45%, COVID-crash rebound). So this is
    # a WARNING requiring human judgement, not an automatic failure; escalating
    # it to PROBLEM would mean the audit can never come back clean.
    a.add(
        "unexplained_jumps",
        WARN,
        f"{len(rows)} move(s) >25% above Rs 5 with no corporate action and no open "
        f"review item ({len(rows)/max(total,1)*100:.4f}% of universe bars); "
        f"{len(severe)} exceed 45% and NEED HUMAN JUDGEMENT — a genuine crash and a "
        "missed action are indistinguishable from the data alone",
        [
            f"{r['symbol']:<12} {r['trade_date']}  {r['move_pct']:+}%  (prior {r['prior_d']})"
            for r in rows
        ],
    )


# --------------------------------------------------- 8. structural invariants
def audit_invariants(a: Audit) -> None:
    # Per ISIN, NOT per (isin, series). A security migrates between series over
    # its life -- SRPL went SM -> EQ -> BE -> BZ -- and cum_factor is computed
    # per ISIN across all of them. The last bar of a DISCONTINUED series
    # legitimately still carries a factor, because the security kept trading
    # elsewhere and later events must still rescale those older bars. Checking
    # per series reported 20 false violations; per ISIN reports zero.
    bad = fetch_all(
        """
        WITH latest AS (
            SELECT DISTINCT ON (isin) isin, symbol, trade_date, cum_factor
              FROM gold.eod_adjusted ORDER BY isin, trade_date DESC
        )
        SELECT symbol, isin, trade_date, cum_factor FROM latest
         WHERE abs(cum_factor - 1.0) > 1e-9 LIMIT 20
        """
    )
    a.add(
        "right_edge_identity",
        OK if not bad else BAD,
        "latest bar of every symbol is unadjusted (adjusted close == printed close)"
        if not bad
        else f"{len(bad)} symbol(s) have cum_factor != 1.0 on their newest bar",
        [f"{r['symbol']} {r['trade_date']} factor={float(r['cum_factor']):.6f}" for r in bad],
    )

    orphan = fetch_one(
        """
        SELECT count(*) AS n FROM gold.eod_adjusted g
         WHERE NOT EXISTS (
            SELECT 1 FROM bronze.eod_bhav_raw b
             WHERE b.trade_date = g.trade_date
               AND b.isin = COALESCE(g.isin_traded, g.isin)
               AND b.series = g.series)
        """
    )
    a.add(
        "gold_traceable_to_bronze",
        OK if not orphan["n"] else BAD,
        "every gold row traces to a bronze bar"
        if not orphan["n"]
        else f"{orphan['n']:,} gold rows have no bronze source",
    )

    # Scoped to the tradeable universe: ABSLBANETF legitimately has two INF*
    # share classes trading under one symbol on the same day. They are ETF units,
    # already excluded from the universe, and are not an identity problem.
    dupes = fetch_all(
        """
        SELECT symbol, trade_date, count(DISTINCT isin) AS n
          FROM gold.eod_adjusted
         WHERE series = 'EQ' AND in_universe
         GROUP BY symbol, trade_date HAVING count(DISTINCT isin) > 1 LIMIT 10
        """
    )
    a.add(
        "one_series_per_symbol",
        OK if not dupes else WARN,
        "no symbol is split across two canonical ISINs on the same day"
        if not dupes
        else f"{len(dupes)} symbol/day pairs map to multiple canonical ISINs",
        [f"{r['symbol']} {r['trade_date']} ({r['n']} isins)" for r in dupes],
    )


# ------------------------------------------------------ 9. per-symbol gaps
def audit_symbol_gaps(a: Audit) -> None:
    """Trading-day gaps inside a symbol's own lifetime.

    A gap is legitimate (suspension) or a missing-data bug; either way a
    strategy must not silently interpolate across it.
    """
    rows = fetch_all(
        """
        WITH td AS (
            SELECT DISTINCT trade_date FROM bronze.eod_bhav_raw
        ),
        indexed AS (
            SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS ix FROM td
        ),
        bars AS (
            SELECT g.isin, g.symbol, i.ix,
                   lag(i.ix) OVER (PARTITION BY g.isin ORDER BY i.ix) AS prev_ix
              FROM gold.eod_adjusted g
              JOIN indexed i ON i.trade_date = g.trade_date
             WHERE g.series = 'EQ'
        )
        SELECT symbol, isin, count(*) AS gap_count, max(ix - prev_ix - 1) AS longest_gap
          FROM bars
         WHERE prev_ix IS NOT NULL AND ix - prev_ix > 1
         GROUP BY symbol, isin
         ORDER BY max(ix - prev_ix - 1) DESC
         LIMIT 25
        """
    )
    if not rows:
        a.add("symbol_internal_gaps", OK, "no symbol has a missing trading day inside its own life")
        return
    long_gaps = [r for r in rows if r["longest_gap"] and r["longest_gap"] > 20]
    a.add(
        "symbol_internal_gaps",
        WARN,
        f"{len(rows)} symbol(s) have internal gaps (suspensions or missing data); "
        f"{len(long_gaps)} exceed 20 trading days",
        [
            f"{r['symbol']:<12} {r['gap_count']} gap(s), longest {r['longest_gap']} trading days"
            for r in rows
        ],
    )


# ----------------------------------------------------- 10. data completeness
def audit_field_completeness(a: Audit) -> None:
    r = fetch_one(
        """
        SELECT count(*) AS n,
               count(*) FILTER (WHERE vwap IS NULL)       AS no_vwap,
               count(*) FILTER (WHERE deliv_qty IS NULL)  AS no_deliv,
               count(*) FILTER (WHERE price_band IS NULL) AS no_band,
               count(*) FILTER (WHERE prev_close IS NULL) AS no_prevclose,
               count(*) FILTER (WHERE trades IS NULL)     AS no_trades
          FROM bronze.eod_bhav_raw
        """
    )
    n = max(r["n"], 1)
    pre2020 = fetch_one(
        "SELECT count(*) AS n FROM bronze.eod_bhav_raw WHERE trade_date < '2020-01-01'"
    )["n"]
    a.add(
        "field_completeness",
        OK,
        f"nulls — vwap {r['no_vwap']/n*100:.1f}% | delivery {r['no_deliv']/n*100:.1f}% | "
        f"band {r['no_band']/n*100:.1f}% | prev_close {r['no_prevclose']/n*100:.1f}%"
        + (f" (note {pre2020:,} pre-2020 bars have no delivery source)" if pre2020 else ""),
    )

    # Delivery nulls are NOT uniform across series, and the reason matters:
    # NSE leaves DELIV_QTY blank for BE/BZ (trade-for-trade) because delivery is
    # 100% by definition under T2T settlement -- there is no intraday netting to
    # report. Reporting that as "missing data" would be misleading.
    per_series = fetch_all(
        """
        SELECT series, count(*) AS n,
               round(100.0 * count(deliv_qty) / count(*), 1) AS pct_deliv,
               round(100.0 * count(vwap) / count(*), 1)      AS pct_vwap
          FROM bronze.eod_bhav_raw
         GROUP BY series ORDER BY 2 DESC LIMIT 10
        """
    )
    a.add(
        "delivery_by_series",
        OK,
        "delivery coverage by series (BE/BZ are blank at source: T2T is 100% delivery by rule):",
        [
            f"{p['series']:<4} {p['n']:>9,} bars  deliv {p['pct_deliv']:>5}%  vwap {p['pct_vwap']:>5}%"
            + ("   <- blank at source, T2T settles 100% delivery" if p["series"] in ("BE", "BZ") else "")
            for p in per_series
        ],
    )

    fmt = fetch_all(
        "SELECT source_format, count(*) AS n, min(trade_date) AS lo, max(trade_date) AS hi "
        "FROM bronze.eod_bhav_raw GROUP BY source_format ORDER BY 2 DESC"
    )
    a.add(
        "source_formats",
        OK,
        "bars by source format:",
        [f"{f['source_format']:<8} {f['n']:>9,} bars  {f['lo']} -> {f['hi']}" for f in fmt],
    )


# -------------------------------------------------------- 11. review backlog
def audit_review_queue(a: Audit) -> None:
    r = fetch_one(
        """
        SELECT count(*) FILTER (WHERE status='open') AS open,
               count(*) FILTER (WHERE status='open' AND severity='high') AS high
          FROM silver.corp_action_review_queue
        """
    )
    by_reason = fetch_all(
        """
        SELECT reason, suggested_type, count(*) AS n
          FROM silver.corp_action_review_queue WHERE status='open' AND severity='high'
         GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10
        """
    )
    a.add(
        "review_queue",
        OK if r["high"] == 0 else WARN,
        f"{r['open']:,} open item(s), {r['high']:,} high severity (price-affecting, unresolved)",
        [f"{b['reason']:<22} {b['suggested_type'] or '?':<20} {b['n']:>4}" for b in by_reason],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    a = Audit()
    for fn in (
        audit_coverage,
        audit_missing_days,
        audit_daily_rowcounts,
        audit_price_sanity,
        audit_prev_close_chain,
        audit_factor_coverage,
        audit_orphaned_factors,
        audit_unexplained_jumps,
        audit_invariants,
        audit_symbol_gaps,
        audit_field_completeness,
        audit_review_queue,
    ):
        try:
            fn(a)
        except Exception as exc:
            a.add(fn.__name__, BAD, f"audit check crashed: {exc}")

    if args.json:
        print(json.dumps(a.results, indent=2, default=str))
    else:
        print("=" * 100)
        print("NSE ADJUSTED PANEL AUDIT")
        print("=" * 100)
        a.render()
        print("=" * 100)
        code = a.exit_code
        print(
            {0: "RESULT: clean", 1: "RESULT: warnings only", 2: "RESULT: PROBLEMS FOUND"}[code]
        )
    return a.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
