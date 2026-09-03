"""Resolve ISIN changes so one entity reads as one continuous series.

A face-value split issues a NEW ISIN. Verified on live NSE data (2026-08):
TDPOWERSYS went INE419M01027 -> INE419M01035 exactly on its split ex-date, and
the new bar's ``prev_close`` equalled the old bar's close to 0.0000%. The same
pattern held for KIRLPNU, TEMBO and CORDELIA -- every split in the window.

Without linkage, two things break silently:
  * the split factor references an ISIN with no bars, so the fake ~-50 % ex-date
    gap survives into the *adjusted* panel, and
  * the symbol appears as two disconnected short histories.

Detection rule (strongest evidence first):
  1. ``prev_close_continuity`` -- adjacent bars, same symbol and series, ISIN
     changed, and the later bar's prev_close matches the earlier bar's close.
     NSE preserves that continuity deliberately, so this is near-conclusive.
  2. ``symbol_match`` -- a corporate-action ISIN with no bars at all, whose
     symbol maps to exactly one canonical ISIN around the ex-date.

The canonical ISIN is the LATEST one in a chain, so the panel is keyed on the
identifier the security actually trades under today.
"""

from __future__ import annotations

import datetime as dt
import json

from ..config import Settings
from ..db import connection, upsert_rows
from ..logging_setup import get_logger

log = get_logger(__name__)

# prev_close vs prior close: NSE matched to 0.0000% in every observed case, so
# this tolerance is generous. Kept non-zero only for float/rounding safety.
CONTINUITY_TOL = 0.001  # 0.1 %

# Chained per SYMBOL across ALL series, not per (symbol, series).
#
# A security often migrates series around the very event that changes its ISIN.
# VINNY split on 2023-02-24 while trading in BE: the BE pair matched exactly
# (15.25 -> 15.25) but the EQ pair spanned four months across the split
# (162.05 -> prev_close 7.00) and was rejected. VERTOZ's 2025 consolidation had
# BOTH per-series pairs rejected for the same reason, which would have left it
# as three disconnected histories with the consolidation unapplied.
#
# Taking ONE row per (symbol, trade_date) -- preferring EQ, then the most liquid
# series -- reconstructs the security's true day-to-day chain regardless of which
# series it happened to trade in.
_DETECT_SQL = """
WITH one_per_day AS (
    SELECT DISTINCT ON (symbol, trade_date)
           symbol, series, trade_date, isin, c, prev_close
      FROM bronze.eod_bhav_raw
     WHERE c IS NOT NULL AND c > 0
       -- Only the security's EQUITY line. Without these two guards, a warrant
       -- (HDFCBANK INE040A13013, series W3) or a debt tranche shares the symbol,
       -- gets picked on a day the equity did not trade, and registers as an ISIN
       -- "switch": 11,574 spurious candidates and an 11k-row review queue.
       AND series IN ('EQ','BE','BZ','SM','ST','T2T')
       AND substr(isin, 8, 2) = '01'
     ORDER BY symbol, trade_date,
              (series = 'EQ') DESC,
              volume DESC NULLS LAST,
              series
),
ord AS (
    SELECT symbol, series, trade_date, isin, c, prev_close,
           lag(isin)       OVER w AS prev_isin,
           lag(c)          OVER w AS prior_c,
           lag(trade_date) OVER w AS prior_d
      FROM one_per_day
    WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
)
SELECT symbol, series, prior_d, prev_isin, trade_date AS switch_date, isin AS new_isin,
       prior_c, prev_close
  FROM ord
 WHERE prev_isin IS NOT NULL
   AND isin <> prev_isin
   AND prior_c IS NOT NULL AND prior_c > 0
   AND prev_close IS NOT NULL AND prev_close > 0
   -- only genuinely adjacent bars; a long gap breaks the continuity evidence
   AND prior_d >= trade_date - INTERVAL '10 days'
 ORDER BY symbol, trade_date
"""


def _canonicalise(edges: dict[str, str]) -> dict[str, str]:
    """Collapse old->new edges into old->final chains.

    ``A -> B`` and ``B -> C`` becomes ``A -> C`` and ``B -> C``. Chains are
    short in practice (IMC1 had three ISINs), but the loop is bounded anyway so
    a cyclic edge from bad data cannot hang the pipeline.
    """
    out: dict[str, str] = {}
    for start in edges:
        seen = {start}
        cur = start
        depth = 0
        while cur in edges and edges[cur] not in seen and depth < 20:
            cur = edges[cur]
            seen.add(cur)
            depth += 1
        out[start] = cur
    return out


def detect_links(settings: Settings | None = None) -> dict[str, int]:
    """Scan bronze for ISIN changes and populate ``silver.isin_link``."""
    stats = {"switches_found": 0, "links_written": 0, "rejected_gap": 0, "isins_total": 0}

    with connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(_DETECT_SQL)
            switches = cur.fetchall()

        edges: dict[str, str] = {}
        symbols: dict[str, str] = {}
        for s in switches:
            prior_c = float(s["prior_c"])
            reported = float(s["prev_close"])
            gap = abs(reported - prior_c) / prior_c
            if gap > CONTINUITY_TOL:
                # prev_close does NOT carry over: this is a different security
                # reusing the ticker, not the same entity re-identified.
                stats["rejected_gap"] += 1
                log.warning(
                    "isin_switch_rejected",
                    symbol=s["symbol"],
                    old=s["prev_isin"],
                    new=s["new_isin"],
                    gap_pct=gap * 100,
                )
                continue
            stats["switches_found"] += 1
            edges[s["prev_isin"]] = s["new_isin"]
            symbols[s["prev_isin"]] = s["symbol"]
            symbols[s["new_isin"]] = s["symbol"]

        chains = _canonicalise(edges)

        # Every ISIN in bronze gets a row, so a single join resolves anything.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT isin,
                       min(trade_date) AS first_seen,
                       max(trade_date) AS last_seen,
                       (array_agg(symbol ORDER BY trade_date DESC))[1] AS symbol
                  FROM bronze.eod_bhav_raw
                 GROUP BY isin
                """
            )
            all_isins = cur.fetchall()
        stats["isins_total"] = len(all_isins)

        rows = []
        for r in all_isins:
            isin = r["isin"]
            canonical = chains.get(isin, isin)
            depth = 0
            cur_i = isin
            while cur_i in edges and depth < 20:
                cur_i = edges[cur_i]
                depth += 1
            rows.append(
                {
                    "isin": isin,
                    "canonical_isin": canonical,
                    "symbol": r["symbol"] or symbols.get(isin),
                    "linked_via": "self" if canonical == isin else "prev_close_continuity",
                    "confidence": "high",
                    "chain_depth": depth,
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                }
            )

        stats["links_written"] = upsert_rows(
            conn,
            "silver.isin_link",
            rows,
            conflict_cols=["isin"],
            update_cols=[
                "canonical_isin", "symbol", "linked_via", "confidence",
                "chain_depth", "first_seen", "last_seen", "updated_at",
            ],
        )

        # Second signal for switches prev_close cannot vouch for.
        stats["stem_ca_links"] = _link_by_stem_and_ca(conn)

        # Re-flatten transitive chains. The stem pass adds edges AFTER the
        # primary pass has already collapsed its own, so a chain can be left
        # half-resolved: VERTOZ had INE188Y01015 -> INE188Y01023 from
        # prev_close continuity, then the stem pass added
        # INE188Y01023 -> INE188Y01031, leaving 01015 pointing at a
        # non-canonical ISIN. Bars under 01015 would then land in a third
        # phantom series.
        stats["chain_collapse_passes"] = _collapse_chains(conn)

        stats["unexplained_switches"] = _flag_unexplained_switches(conn, switches)

    log.info("isin_links_detected", **stats)
    return stats


# Indian ISINs are ``IN`` + issuer/security code + 2 type digits + check digit.
# The first 9 characters therefore identify the ISSUER AND SECURITY; only the
# type digits and check digit move when a face-value change reissues it:
#     INE188Y01023 -> INE188Y01031   (VERTOZ, Re 1 -> Rs 10 consolidation)
_ISIN_STEM_LEN = 9

# An Indian ISIN is: IN + E/9/0 + 4-char issuer + 2-digit SECURITY TYPE +
# 2-digit serial + check digit. The type digits sit at positions 8-9 (1-indexed),
# NOT 10-11:
#     INE040A01034 -> type '01' (equity)      INE614X07142 -> type '07' (debt)
#     INE040A13013 -> type '13' (warrant)
# Getting the offset wrong silently matched nothing and detection found 0 switches.
# #     01 = equity shares      07 = debentures / bonds
#     13/14 = warrants        20 = mutual fund units
# The stem rule must require '01' on BOTH sides. Without it, every NCD tranche of
# one issuer shares a stem and gets merged: DHANILOANS (INE614X07142,
# INE614X07233, INE614X07266, ...) and DHFL (INE202B07IY2, INE202B07JC6) were
# wrongly chained together, collapsing distinct bonds into one series.
_EQUITY_TYPE = "01"

_STEM_CA_SQL = f"""
WITH spans AS (
    SELECT isin,
           (array_agg(symbol ORDER BY trade_date DESC))[1] AS symbol,
           min(trade_date) AS first_seen,
           max(trade_date) AS last_seen
      FROM bronze.eod_bhav_raw
     -- only series that reach the panel; debt/GS never link
     WHERE series IN ('EQ','BE','BZ','SM','ST','T2T')
       AND substr(isin, 8, 2) = '{_EQUITY_TYPE}'
     GROUP BY isin
),
pairs AS (
    SELECT o.isin AS old_isin, n.isin AS new_isin, o.symbol,
           o.last_seen AS old_last, n.first_seen AS new_first
      FROM spans o
      JOIN spans n
        ON n.isin <> o.isin
       AND left(n.isin, {_ISIN_STEM_LEN}) = left(o.isin, {_ISIN_STEM_LEN})
       AND n.symbol = o.symbol
       -- the new ISIN takes over where the old one stops
       AND n.first_seen > o.last_seen
       AND n.first_seen <= o.last_seen + INTERVAL '120 days'
     WHERE NOT EXISTS (
            SELECT 1 FROM silver.isin_link l
             WHERE l.isin = o.isin AND l.canonical_isin <> o.isin
           )
)
SELECT p.old_isin, p.new_isin, p.symbol, p.old_last, p.new_first,
       (SELECT string_agg(DISTINCT e.type, ',')
          FROM silver.corp_action_event e
         WHERE COALESCE(e.canonical_isin, e.isin) IN (p.old_isin, p.new_isin)
            OR e.symbol = p.symbol
           AND e.ex_date > p.old_last AND e.ex_date <= p.new_first
           AND e.type IN ('SPLIT','FACE_VALUE_CHANGE','BONUS','RIGHTS')
       ) AS ca_types
  FROM pairs p
 ORDER BY p.symbol, p.old_last
"""


def _collapse_chains(conn, max_passes: int = 10) -> int:
    """Point every ISIN at the FINAL canonical of its chain.

    Iterative rather than recursive-CTE so a cyclic edge from bad data cannot
    loop forever; chains are 2-3 long in practice, so it converges in one or two
    passes. Returns the number of passes actually needed.
    """
    passes = 0
    for _ in range(max_passes):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE silver.isin_link a
                   SET canonical_isin = b.canonical_isin,
                       chain_depth = a.chain_depth + 1,
                       updated_at = now()
                  FROM silver.isin_link b
                 WHERE a.canonical_isin = b.isin
                   AND b.canonical_isin <> b.isin
                   AND a.isin <> b.canonical_isin
                """
            )
            n = cur.rowcount or 0
        passes += 1
        if n == 0:
            break
        log.info("isin_chain_collapsed", pass_no=passes, rows=n)
    return passes


def _link_by_stem_and_ca(conn) -> int:
    """Link ISINs that share an issuer stem when a share-count event explains it.

    ``prev_close`` continuity is the primary and near-conclusive signal, but it
    is unavailable when the security is SUSPENDED across the event. VERTOZ was
    halted 2025-06-25..2025-07-10 for a Re 1 -> Rs 10 consolidation; on
    resumption NSE reported prev_close 9.81 (a reference price) against a last
    traded close of 9.17, so the primary rule rejected the pair and VERTOZ read
    as two disconnected securities with the consolidation unapplied.

    This fallback requires THREE things to agree -- same symbol, same 9-character
    ISIN stem, and the new ISIN starting within 120 days of the old one ending --
    and records ``confidence = 'medium'`` so the weaker evidence stays visible.
    It never overrides a link the primary rule already made.
    """
    with conn.cursor() as cur:
        cur.execute(_STEM_CA_SQL)
        pairs = cur.fetchall()

    if not pairs:
        return 0

    linked = 0
    for p in pairs:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE silver.isin_link
                   SET canonical_isin = %s,
                       linked_via = 'symbol_match',
                       confidence = 'medium',
                       chain_depth = 1,
                       note = %s,
                       updated_at = now()
                 WHERE isin = %s
                   AND canonical_isin = isin
                """,
                (
                    p["new_isin"],
                    (
                        f"ISIN stem match {p['old_isin']}->{p['new_isin']}; old series ended "
                        f"{p['old_last']}, new began {p['new_first']}; "
                        f"corp actions in window: {p['ca_types'] or 'none found'}. "
                        "prev_close continuity unavailable (suspended across the event)."
                    ),
                    p["old_isin"],
                ),
            )
            if cur.rowcount:
                linked += cur.rowcount
                log.info(
                    "isin_linked_by_stem",
                    symbol=p["symbol"],
                    old=p["old_isin"],
                    new=p["new_isin"],
                    old_last=str(p["old_last"]),
                    new_first=str(p["new_first"]),
                    ca_types=p["ca_types"],
                )
    return linked


def _flag_unexplained_switches(conn, switches: list[dict]) -> int:
    """Queue ISIN switches that have no corporate action to explain them.

    An ISIN switch is strong evidence of a share-count event, and the raw price
    ratio across the switch usually reveals the exact ratio. But INFERRING a
    corporate action from a price move is precisely the guessing the design
    forbids, so the ratio goes into ``suggested_json`` for a human and is NEVER
    auto-applied.

    Real case: GOLDADD and SILVERADD (ETF units) executed ~10:1 unit splits on
    2026-08-28 that NSE does not publish in the equity corporate-actions feed.
    """
    if not switches:
        return 0

    rows = []
    for s in switches:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM silver.corp_action_event
                 WHERE ex_date = %s
                   AND superseded_at IS NULL
                   AND (symbol = %s OR isin IN (%s, %s))
                   AND type IN ('BONUS','SPLIT','FACE_VALUE_CHANGE','RIGHTS',
                                'DEMERGER','SCHEME_OF_ARRANGEMENT')
                 LIMIT 1
                """,
                (s["switch_date"], s["symbol"], s["prev_isin"], s["new_isin"]),
            )
            if cur.fetchone():
                continue  # explained

        prior_c = float(s["prior_c"])
        new_c = None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c FROM bronze.eod_bhav_raw WHERE isin = %s AND trade_date = %s",
                (s["new_isin"], s["switch_date"]),
            )
            r = cur.fetchone()
            if r and r["c"]:
                new_c = float(r["c"])

        implied = (new_c / prior_c) if (new_c and prior_c) else None
        rows.append(
            {
                "ca_raw_id": None,
                "isin": s["new_isin"],
                "symbol": s["symbol"],
                "ex_date": s["switch_date"],
                "raw_purpose": (
                    f"[derived] ISIN changed {s['prev_isin']} -> {s['new_isin']} "
                    f"on {s['switch_date']} with no corporate action on file"
                ),
                "reason": "unparsed_pattern",
                "parser_version": "isin_link/1.0.0",
                "suggested_type": "SPLIT",
                "suggested_json": json.dumps(
                    {
                        "old_isin": s["prev_isin"],
                        "new_isin": s["new_isin"],
                        "prior_close": prior_c,
                        "switch_close": new_c,
                        "implied_price_ratio": implied,
                        "note": "NOT auto-applied; confirm the ratio and add a corp_action_override",
                    }
                ),
                "severity": "high",
            }
        )

    if not rows:
        return 0
    from ..db import REVIEW_QUEUE_CONFLICT, insert_ignore

    insert_ignore(
        conn,
        "silver.corp_action_review_queue",
        rows,
        ["ca_raw_id", "raw_purpose"],
        conflict_target=REVIEW_QUEUE_CONFLICT,
    )
    for r in rows:
        log.warning(
            "isin_switch_unexplained",
            symbol=r["symbol"],
            ex_date=str(r["ex_date"]),
            suggested=r["suggested_json"],
        )
    return len(rows)


def resolve_event_isins(settings: Settings | None = None) -> dict[str, int]:
    """Fill ``canonical_isin`` on every corporate-action event.

    Resolution order:
      1. the event's ISIN maps through ``isin_link``  -> ``link``
      2. the ISIN has bars but no link row            -> ``direct``
      3. the ISIN has NO bars, but the symbol resolves
         to exactly one canonical ISIN               -> ``symbol_match``
      4. otherwise leave NULL                        -> ``unresolved``

    Step 3 matters: 22 of 60 price-adjusting events referenced an ISIN with no
    bars at all, because the corp-action feed reports a superseded identifier.
    Requiring uniqueness keeps it from guessing when a ticker was reused.
    """
    stats = {"link": 0, "direct": 0, "symbol_match": 0, "unresolved": 0}

    with connection(settings) as conn:
        # 1 + 2: resolve straight through the link table.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE silver.corp_action_event e
                   SET canonical_isin = l.canonical_isin,
                       isin_resolved_via = CASE
                           WHEN l.canonical_isin = e.isin THEN 'direct' ELSE 'link' END,
                       updated_at = now()
                  FROM silver.isin_link l
                 WHERE l.isin = e.isin
                   AND (e.canonical_isin IS DISTINCT FROM l.canonical_isin)
                """
            )
            resolved = cur.rowcount or 0

            cur.execute(
                """
                SELECT isin_resolved_via AS via, count(*) AS n
                  FROM silver.corp_action_event
                 WHERE canonical_isin IS NOT NULL
                 GROUP BY 1
                """
            )
            for r in cur.fetchall():
                if r["via"] in stats:
                    stats[r["via"]] = r["n"]

        # 3: symbol fallback for events whose ISIN never appears in bronze.
        #
        # The corp-action feed frequently reports a SUPERSEDED identifier, and
        # sometimes not an ISIN at all:
        #   HDFCBANK's 2025 Bonus 1:1 was filed under INE040A01018, the
        #     pre-merger ISIN; every bar is under INE040A01034.
        #   VERTOZ's 2020 bonus was filed with isin = "314265", an internal id.
        #
        # Requiring the symbol to map to exactly ONE canonical ISIN globally was
        # too strict: HDFCBANK also has INE040A13013, a WARRANT (series W3, 18
        # bars in 2023), which made the symbol "ambiguous" and left HDFC Bank's
        # 1:1 bonus unapplied -- its whole pre-2025 history unadjusted by 2x on
        # India's largest private bank.
        #
        # So candidates are restricted to equity-like series that actually reach
        # the panel, AND the ISIN's traded span must straddle the ex-date. That
        # is real evidence the event belongs to that security, not a guess.
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH eq_bars AS (
                    SELECT b.symbol, b.trade_date,
                           COALESCE(l.canonical_isin, b.isin) AS canon
                      FROM bronze.eod_bhav_raw b
                      LEFT JOIN silver.isin_link l ON l.isin = b.isin
                     WHERE b.series IN ('EQ','BE','BZ','SM','ST','T2T')
                       AND substr(b.isin, 8, 2) = '01'
                ),
                -- Span per CANONICAL ISIN, NOT per (canonical, symbol).
                -- A symbol RENAME otherwise fragments the security's timeline:
                -- URAVI was renamed URAVIDEF in Jan 2025, and NSE filed its
                -- 2022 Bonus 1:1 under the NEW symbol. Scoping the span to
                -- URAVIDEF's own bars (2025 onward) excluded the 2022 ex-date,
                -- so the bonus never resolved and URAVI showed a -47% phantom
                -- crash. The ISIN's full span (2020-2026) contains it.
                spans AS (
                    SELECT canon,
                           min(trade_date) AS first_seen,
                           max(trade_date) AS last_seen,
                           count(*)        AS bars
                      FROM eq_bars GROUP BY canon
                ),
                sym2canon AS (
                    SELECT DISTINCT symbol, canon FROM eq_bars
                ),
                ranked AS (
                    SELECT e.event_id, s.canon,
                           row_number() OVER (
                               PARTITION BY e.event_id
                               ORDER BY s.bars DESC, s.first_seen
                           ) AS rn,
                           count(*) OVER (PARTITION BY e.event_id) AS n_cand
                      FROM silver.corp_action_event e
                      JOIN sym2canon sc ON sc.symbol = e.symbol
                      JOIN spans s      ON s.canon = sc.canon
                       -- the security must have been trading around the ex-date
                     WHERE e.canonical_isin IS NULL
                       AND e.symbol IS NOT NULL
                       AND e.ex_date BETWEEN s.first_seen - INTERVAL '10 days'
                                         AND s.last_seen  + INTERVAL '400 days'
                )
                UPDATE silver.corp_action_event e
                   SET canonical_isin = r.canon,
                       isin_resolved_via = CASE
                           WHEN r.n_cand = 1 THEN 'symbol_match'
                           ELSE 'symbol_match'  -- most-traded candidate
                       END,
                       updated_at = now()
                  FROM ranked r
                 WHERE r.event_id = e.event_id
                   AND r.rn = 1
                """
            )
            stats["symbol_match"] = cur.rowcount or 0

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM silver.corp_action_event WHERE canonical_isin IS NULL"
            )
            stats["unresolved"] = cur.fetchone()["n"]

    log.info("event_isins_resolved", **stats)
    return stats


def sync_isin_history(settings: Settings | None = None) -> int:
    """Record each entity's ISIN chain on its security_master row."""
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE silver.security_master sm
               SET isin_history = COALESCE(x.hist, '[]'::jsonb),
                   updated_at = now()
              FROM (
                SELECT l.canonical_isin,
                       jsonb_agg(
                           jsonb_build_object(
                               'isin', l.isin,
                               'first_seen', l.first_seen,
                               'last_seen', l.last_seen,
                               'linked_via', l.linked_via
                           ) ORDER BY l.first_seen
                       ) AS hist
                  FROM silver.isin_link l
                 GROUP BY l.canonical_isin
                HAVING count(*) > 1
              ) x
             WHERE sm.isin = x.canonical_isin
            """
        )
        return cur.rowcount or 0


def refresh(settings: Settings | None = None) -> dict:
    """Full linkage refresh: detect, resolve events, sync history."""
    d = detect_links(settings)
    r = resolve_event_isins(settings)
    n = sync_isin_history(settings)
    return {"detect": d, "resolve": r, "history_rows": n}


def canonical_map(isins: list[str], settings: Settings | None = None) -> dict[str, str]:
    """Map the given ISINs to their canonical form (identity when unknown)."""
    if not isins:
        return {}
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT isin, canonical_isin FROM silver.isin_link WHERE isin = ANY(%s)",
            (isins,),
        )
        m = {r["isin"]: r["canonical_isin"] for r in cur.fetchall()}
    return {i: m.get(i, i) for i in isins}


def expand_to_all_isins(canonical: list[str], settings: Settings | None = None) -> list[str]:
    """Given canonical ISINs, return every ISIN that maps to them.

    Needed when rebuilding gold: the bars for a post-split entity live under
    BOTH the old and the new ISIN in bronze.
    """
    if not canonical:
        return []
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT isin FROM silver.isin_link WHERE canonical_isin = ANY(%s)",
            (canonical,),
        )
        found = [r["isin"] for r in cur.fetchall()]
    return sorted(set(found) | set(canonical))
