-- 007_quality_gate.sql
--
-- Makes it impossible for the trading layer to read unverified prices.
--
-- Motivating incident: HDFC Bank's 2025 Bonus 1:1 was filed by NSE against
-- INE040A01018 (the pre-merger ISIN) while every bar sits under INE040A01034.
-- The factor parsed, computed to exactly 0.500000, and reached
-- factor_state='computed'. Every check passed -- right_edge_identity was
-- consistently 1.0, stale_factor saw a factor present -- because the factor
-- multiplied ZERO rows. India's largest private bank carried its entire
-- pre-August-2025 history unadjusted by 2x.
--
-- Two structural answers here:
--   1. security_quality: a per-ISIN verification status, derived from open
--      high-severity review items, orphaned factors, and unresolved
--      adjusted-prev_close breaks.
--   2. gold.tradeable_universe: a MATERIALIZED table (not a view) that only the
--      verified names enter. It is rebuilt inside run-daily AFTER the panel is
--      built and all checks pass. A view would expose unverified prices the
--      instant gold was written; a table leaves the last known-good universe in
--      place when a run fails, which is the whole point of the gate.
BEGIN;

-- ------------------------------------------------------- silver.security_quality
CREATE TABLE IF NOT EXISTS silver.security_quality (
    isin                text        PRIMARY KEY,
    symbol              text,
    status              text        NOT NULL DEFAULT 'review_open',
    open_high_sev       integer     NOT NULL DEFAULT 0,
    orphaned_factors    integer     NOT NULL DEFAULT 0,
    unresolved_breaks   integer     NOT NULL DEFAULT 0,
    last_reviewed       timestamptz,
    note                text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT security_quality_status_ck
        CHECK (status IN ('verified', 'review_open', 'blocked'))
);

CREATE INDEX IF NOT EXISTS security_quality_status_idx
    ON silver.security_quality (status);
CREATE INDEX IF NOT EXISTS security_quality_verified_idx
    ON silver.security_quality (isin) WHERE status = 'verified';

COMMENT ON TABLE silver.security_quality IS
  'Per-ISIN verification status consumed by the trading layer. Keyed on the CANONICAL isin, matching gold.eod_adjusted.';
COMMENT ON COLUMN silver.security_quality.status IS
  'verified = no open high-severity review item, no orphaned factor, no unresolved prev_close break. review_open = something outstanding. blocked = manually held out regardless of checks.';
COMMENT ON COLUMN silver.security_quality.orphaned_factors IS
  'Count of non-identity factors that multiply zero rows for this ISIN. Detected chain-aware through silver.isin_link, because the canonical ISIN is the LATEST in a chain and legitimately has no bars before an early ex-date.';

-- ---------------------------------------------------- gold.tradeable_universe
-- What the momentum tool reads. Only verified names appear, so an unverified
-- security is not "flagged" downstream -- it simply is not a candidate.
CREATE TABLE IF NOT EXISTS gold.tradeable_universe (
    trade_date      date            NOT NULL,
    isin            text            NOT NULL,
    symbol          text            NOT NULL,
    series_flag     text,
    adv_20          double precision,
    c_adj           double precision,
    quality_status  text            NOT NULL,
    built_at        timestamptz     NOT NULL DEFAULT now(),
    CONSTRAINT tradeable_universe_pk PRIMARY KEY (trade_date, isin)
);

CREATE INDEX IF NOT EXISTS tradeable_universe_date_idx
    ON gold.tradeable_universe (trade_date);
CREATE INDEX IF NOT EXISTS tradeable_universe_isin_idx
    ON gold.tradeable_universe (isin, trade_date);

COMMENT ON TABLE gold.tradeable_universe IS
  'THE gate. gold.eod_adjusted.in_universe (series + liquidity + not delisted) INTERSECTED with security_quality.status = verified. Rebuilt in run-daily only after every validation check passes, so a failed run cannot widen it.';
COMMENT ON COLUMN gold.tradeable_universe.built_at IS
  'When this row was last written. A stale built_at means run-daily has not completed cleanly since then -- check `nse-eod last-success`.';

-- ------------------------------------- review queue: accept a genuine move
-- 'reviewed_accepted' closes a large-move item that a human confirmed is a real
-- price move rather than a missed corporate action. Distinct from 'resolved',
-- which means a factor or re-anchor was actually applied.
ALTER TABLE silver.corp_action_review_queue
    DROP CONSTRAINT IF EXISTS carq_status_ck;
ALTER TABLE silver.corp_action_review_queue
    ADD CONSTRAINT carq_status_ck
    CHECK (status IN ('open', 'resolved', 'ignored', 'reviewed_accepted'));

-- Where a human-supplied external number came from. Required for a
-- demerger/spin-off resolution: the factor cannot be derived from NSE EOD data,
-- so the provenance of the number IS the audit trail.
ALTER TABLE silver.corp_action_review_queue
    ADD COLUMN IF NOT EXISTS resolution_source text;
COMMENT ON COLUMN silver.corp_action_review_queue.resolution_source IS
  'Provenance of a human-supplied number, e.g. "NSE demerger circular 2026/041" or "special pre-open price 2026-07-22". Mandatory for demerger/spin-off resolutions.';

-- `reason` deliberately has NO check constraint (free text). Values in use:
--   unparsed_pattern | ambiguous_ratio | needs_external_price | s_gt_p
--   no_anchor_price  | multi_component_partial | implausible_factor
--   orphaned_factor        (007) factor multiplies zero rows -- wrong anchor
--   pre_history            (007) ex-date precedes all coverage; nothing to adjust
--   inferred_isin_switch   (007) ISIN changed with no corporate action to explain it
--   large_move_unexplained (007) >25% adjusted move needing external confirmation
COMMENT ON COLUMN silver.corp_action_review_queue.reason IS
  'Free text, no CHECK. See migration 007 for the catalogue. The three categories that need DIFFERENT human input: needs_external_price (demerger -> spun-off value), inferred_isin_switch (-> re-anchor), large_move_unexplained (-> confirm genuine).';

-- ------------------------------------------------------- ops.v_last_success
-- DERIVED, not stored. A `last_success` column on pipeline_run would be a
-- denormalised copy of the rows it summarises and could drift from them; the
-- dead-man's switch must not be able to lie about when the pipeline last ran.
CREATE OR REPLACE VIEW ops.v_last_success AS
SELECT
    (SELECT max(finished_at) FROM ops.pipeline_run
      WHERE command = 'run-daily' AND status IN ('success', 'warning')
        AND finished_at IS NOT NULL)                     AS last_success_at,
    (SELECT max(target_date) FROM ops.pipeline_run
      WHERE command = 'run-daily' AND status IN ('success', 'warning'))
                                                         AS last_success_trade_date,
    (SELECT max(finished_at) FROM ops.pipeline_run
      WHERE command = 'run-daily' AND finished_at IS NOT NULL)
                                                         AS last_attempt_at,
    (SELECT status FROM ops.pipeline_run
      WHERE command = 'run-daily' AND finished_at IS NOT NULL
      ORDER BY finished_at DESC LIMIT 1)                  AS last_attempt_status,
    (SELECT max(built_at) FROM gold.tradeable_universe)   AS universe_built_at;

COMMENT ON VIEW ops.v_last_success IS
  'Dead-man''s switch source. `status IN (success, warning)` counts as success on purpose: run-daily exits 1 for warnings, which is a normal completed run, not a failure to run.';

-- Verified-universe summary, for `nse-eod universe-status`.
CREATE OR REPLACE VIEW ops.v_universe_status AS
SELECT q.status,
       count(*)                                                AS securities,
       count(*) FILTER (WHERE q.open_high_sev > 0)             AS with_open_high_sev,
       count(*) FILTER (WHERE q.orphaned_factors > 0)          AS with_orphaned_factor,
       count(*) FILTER (WHERE q.unresolved_breaks > 0)         AS with_unresolved_break
  FROM silver.security_quality q
 GROUP BY q.status;

COMMIT;
