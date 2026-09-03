-- 003_gold.sql — the materialized adjusted panel. Fully derived; safe to drop and rebuild
-- from bronze.eod_bhav_raw + silver.corp_action_event alone.
BEGIN;

CREATE TABLE IF NOT EXISTS gold.eod_adjusted (
    trade_date      date            NOT NULL,
    isin            text            NOT NULL,
    symbol          text            NOT NULL,   -- symbol as at trade_date (point-in-time)
    series          text            NOT NULL,

    -- price-adjusted series (splits/bonus/rights/special cash only)
    o_adj           double precision,
    h_adj           double precision,
    l_adj           double precision,
    c_adj           double precision,
    v_adj           double precision,

    -- parallel TOTAL-RETURN series: as above, but ALL dividends reinvested at ex-date.
    -- Benchmarking only. Never use for signal generation.
    o_tr            double precision,
    h_tr            double precision,
    l_tr            double precision,
    c_tr            double precision,

    -- provenance / raw passthrough
    c_raw           double precision,           -- unadjusted close, for reconciliation
    cum_factor      double precision NOT NULL,  -- product of factor_price for ex_date > trade_date
    cum_factor_vol  double precision NOT NULL,
    cum_factor_tr   double precision NOT NULL,  -- total-return cumulative factor

    volume          bigint,
    turnover        double precision,
    vwap_adj        double precision,
    deliv_pct       double precision,

    in_universe     boolean         NOT NULL DEFAULT false,  -- EQ + not delisted + liquidity floor
    tradeable       boolean         NOT NULL DEFAULT false,  -- series EQ only
    series_flag     text,                                    -- EQ|BE|BZ|T2T|SM|ST|IV|other
    adv_20          double precision,                        -- 20d avg turnover, rupees
    circuit_flag    boolean,
    price_band      text,

    -- audit
    factor_asof     timestamptz,        -- max(learned_at) of events folded into cum_factor
    updated_at      timestamptz     NOT NULL DEFAULT now(),

    CONSTRAINT eod_adjusted_pk PRIMARY KEY (trade_date, isin, series),
    CONSTRAINT eod_adjusted_cf_ck CHECK (cum_factor > 0)
);

CREATE INDEX IF NOT EXISTS eod_adj_isin_date_idx ON gold.eod_adjusted (isin, trade_date);
CREATE INDEX IF NOT EXISTS eod_adj_date_idx      ON gold.eod_adjusted (trade_date);
CREATE INDEX IF NOT EXISTS eod_adj_universe_idx  ON gold.eod_adjusted (trade_date, in_universe)
    WHERE in_universe;
CREATE INDEX IF NOT EXISTS eod_adj_symbol_idx    ON gold.eod_adjusted (symbol, trade_date);

COMMENT ON TABLE gold.eod_adjusted IS
  'Derived nightly: raw x cumulative factor. A single new ex-date rescales an entire symbol history, so this table is REBUILT per affected ISIN rather than appended to.';
COMMENT ON COLUMN gold.eod_adjusted.cum_factor IS
  'Product of factor_price over all non-superseded events with ex_date > trade_date. Equals 1.0 for the most recent bar of every symbol.';
COMMENT ON COLUMN gold.eod_adjusted.factor_asof IS
  'Latest learned_at folded in. If a factor is learned later, this row is recomputed and factor_asof advances — the audit trail for "why did history change".';

COMMIT;
