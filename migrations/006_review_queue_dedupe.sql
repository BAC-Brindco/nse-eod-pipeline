-- 006_review_queue_dedupe.sql
--
-- Bug found by running the daily cycle three times: the open review-queue count
-- grew 6 -> 8 -> 10, one duplicate pair per run.
--
-- Cause: `CONSTRAINT carq_uq UNIQUE (ca_raw_id, raw_purpose)` does NOT dedupe
-- rows whose ca_raw_id is NULL, because in SQL NULL is never equal to NULL, so
-- two NULL-keyed rows never "conflict" and ON CONFLICT DO NOTHING never fires.
--
-- Derived review items (an unexplained ISIN switch, detected from bronze rather
-- than from a corporate-action announcement) legitimately have no ca_raw_id, so
-- they duplicated on every run. Left alone, the queue grows without bound and
-- the alert count inflates until the channel gets muted.
--
-- Fix: a unique index over COALESCE(ca_raw_id, -1), which makes NULLs collide
-- with each other as intended.
BEGIN;

-- Collapse the duplicates already written, keeping the earliest of each group
-- (its created_at is the true first-seen time).
DELETE FROM silver.corp_action_review_queue q
 USING silver.corp_action_review_queue keep
 WHERE q.review_id > keep.review_id
   AND COALESCE(q.ca_raw_id, -1) = COALESCE(keep.ca_raw_id, -1)
   AND q.raw_purpose = keep.raw_purpose;

ALTER TABLE silver.corp_action_review_queue
    DROP CONSTRAINT IF EXISTS carq_uq;

CREATE UNIQUE INDEX IF NOT EXISTS carq_uq_idx
    ON silver.corp_action_review_queue (COALESCE(ca_raw_id, -1), raw_purpose);

COMMENT ON INDEX silver.carq_uq_idx IS
  'COALESCE, not a plain UNIQUE: derived review items have a NULL ca_raw_id, and NULL <> NULL would let them duplicate on every run.';

-- ca_raw_id is genuinely optional for derived items.
ALTER TABLE silver.corp_action_review_queue
    ALTER COLUMN ca_raw_id DROP NOT NULL;

COMMIT;
