-- 005_isin_linkage.sql
--
-- A face-value split CHANGES THE ISIN. Measured on live NSE data, 2026-08:
--
--   symbol      prior_day    old ISIN       switch_date  new ISIN       gap
--   TDPOWERSYS  2026-08-21   INE419M01027   2026-08-24   INE419M01035   0.0000%
--   KIRLPNU     2026-08-17   INE811A01020   2026-08-18   INE811A01038   0.0000%
--   TEMBO       2026-08-04   INE869Y01010   2026-08-05   INE869Y01028   0.0000%
--   CORDELIA    2026-08-24   INE0LZF01013   2026-08-25   INE0LZF01039   0.0000%
--
-- In every case the switch date IS the split ex-date, and the new bar's
-- prev_close equals the old bar's close exactly -- NSE preserves the price
-- continuity even though the identifier changes.
--
-- Consequences if unhandled (both were observed before this migration existed):
--   1. 22 of 60 price-adjusting events referenced an ISIN with NO bars at all,
--      so the split factor attached to nothing and the fake ~-50% ex-date gap
--      survived into the "adjusted" panel.
--   2. Each affected symbol appeared as TWO disconnected short histories rather
--      than one continuous series.
--
-- "Master key is ISIN, never symbol" still holds -- but the ISIN must first be
-- resolved to a CANONICAL one. That is what this table provides.
BEGIN;

CREATE TABLE IF NOT EXISTS silver.isin_link (
    isin            text        PRIMARY KEY,          -- any ISIN ever seen
    canonical_isin  text        NOT NULL,             -- the entity's current ISIN
    symbol          text,
    linked_via      text        NOT NULL,
        -- self | prev_close_continuity | symbol_match | manual
    confidence      text        NOT NULL DEFAULT 'high',
    chain_depth     smallint    NOT NULL DEFAULT 0,   -- 0 = is itself canonical
    first_seen      date,
    last_seen       date,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT isin_link_conf_ck CHECK (confidence IN ('high','medium','low')),
    CONSTRAINT isin_link_via_ck  CHECK (
        linked_via IN ('self','prev_close_continuity','symbol_match','manual')
    )
);

CREATE INDEX IF NOT EXISTS isin_link_canonical_idx ON silver.isin_link (canonical_isin);
CREATE INDEX IF NOT EXISTS isin_link_symbol_idx    ON silver.isin_link (symbol);

COMMENT ON TABLE silver.isin_link IS
  'Maps every ISIN ever observed to the entity''s current (canonical) ISIN. A face-value split issues a new ISIN; without this map the split factor cannot find its own price history.';
COMMENT ON COLUMN silver.isin_link.linked_via IS
  'prev_close_continuity: adjacent bars, same symbol+series, ISIN changed, and the new bar''s prev_close equals the old close -- the strongest available evidence. symbol_match: a corp-action ISIN absent from bronze resolved through its unique symbol.';

-- Resolved ISIN on each event, so anchor lookup and materialization can join
-- straight to bronze without repeating the resolution logic.
ALTER TABLE silver.corp_action_event
    ADD COLUMN IF NOT EXISTS canonical_isin text;
ALTER TABLE silver.corp_action_event
    ADD COLUMN IF NOT EXISTS isin_resolved_via text;

CREATE INDEX IF NOT EXISTS cae_canonical_isin_idx
    ON silver.corp_action_event (canonical_isin, ex_date)
 WHERE superseded_at IS NULL;

COMMENT ON COLUMN silver.corp_action_event.canonical_isin IS
  'The ISIN whose bars this event actually adjusts. Differs from `isin` whenever the corp-action feed reports a superseded identifier.';

-- Gold keeps the as-traded identifier for provenance while being keyed on the
-- canonical one, so a symbol reads as ONE continuous series across a split.
ALTER TABLE gold.eod_adjusted
    ADD COLUMN IF NOT EXISTS isin_traded text;

COMMENT ON COLUMN gold.eod_adjusted.isin IS
  'CANONICAL isin: stable across face-value splits, so one entity is one series.';
COMMENT ON COLUMN gold.eod_adjusted.isin_traded IS
  'The ISIN actually printed in the bhavcopy for this bar. Differs from `isin` on bars before an ISIN-changing event.';

-- Every ISIN and how it resolves, for inspection.
CREATE OR REPLACE VIEW ops.v_isin_chains AS
SELECT l.canonical_isin,
       count(*)                                   AS isin_count,
       string_agg(l.isin, ' -> ' ORDER BY l.first_seen) AS chain,
       min(l.first_seen)                          AS first_seen,
       max(l.last_seen)                           AS last_seen,
       max(l.symbol)                              AS symbol
  FROM silver.isin_link l
 GROUP BY l.canonical_isin
HAVING count(*) > 1;

COMMIT;
