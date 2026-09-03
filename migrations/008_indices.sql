-- NSE index daily closes, including India VIX.
--
-- SOURCE
--   https://nsearchives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv
--   One file per trading day, ~165 index rows, verified live on 2026-09-03 back to
--   2018-06-29. Weekends and holidays 404, which is the same non-trading-day signal
--   the equity bhavcopy gives, so absence is information rather than a gap.
--
-- WHY THIS EXISTS
--   Two gaps closed by one file. The equity bhavcopy carries no index rows, so the
--   pipeline had no India VIX (the only INDIAVIX rows anywhere were FUTIVX -- VIX
--   futures from a product delisted in 2015) and no index level, which forced the
--   momentum tracker to use the NIFTYBEES ETF as a benchmark proxy.
--
-- WHY BRONZE IS SEPARATE FROM SILVER
--   Same three-store discipline as the equity path: bronze holds the file's own text
--   verbatim so a parser fix can be replayed without re-fetching seven years of
--   archives, silver holds typed values.

CREATE TABLE IF NOT EXISTS bronze.index_close_raw (
    trade_date    date NOT NULL,
    index_name    text NOT NULL,          -- exactly as printed, casing included
    -- Every value column stays TEXT here. India VIX prints "-" for volume, turnover,
    -- P/E, P/B and dividend yield; coercing at ingest would either fail or silently
    -- write 0, and 0 turnover is a number a downstream filter would believe.
    open_txt      text,
    high_txt      text,
    low_txt       text,
    close_txt     text,
    points_chg    text,
    pct_chg       text,
    volume_txt    text,
    turnover_txt  text,
    pe_txt        text,
    pb_txt        text,
    div_yield_txt text,
    source_file   text NOT NULL,
    row_hash      text NOT NULL,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT index_close_raw_pk PRIMARY KEY (trade_date, index_name)
);

-- Typed, queryable daily series. index_key is the normalised join key: the source
-- prints "Nifty 50", "NIFTY Midcap 100" and "India VIX" with inconsistent casing and
-- spacing, so matching on the raw name would be a casing bug waiting to happen.
CREATE TABLE IF NOT EXISTS silver.index_daily (
    trade_date  date NOT NULL,
    index_key   text NOT NULL,            -- upper-cased, single-spaced: "NIFTY 50"
    index_name  text NOT NULL,            -- as printed, for display
    open        double precision,
    high        double precision,
    low         double precision,
    close       double precision,
    points_chg  double precision,
    pct_chg     double precision,
    volume      bigint,
    turnover_cr double precision,
    pe          double precision,
    pb          double precision,
    div_yield   double precision,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT index_daily_pk PRIMARY KEY (trade_date, index_key)
);

CREATE INDEX IF NOT EXISTS index_daily_key_date_ix
    ON silver.index_daily (index_key, trade_date DESC);

-- Convenience views. Named for what they are so a consumer does not have to know the
-- exact casing NSE happened to print on any given day.
CREATE OR REPLACE VIEW silver.v_india_vix AS
    SELECT trade_date, open, high, low, close, points_chg, pct_chg
      FROM silver.index_daily
     WHERE index_key = 'INDIA VIX';

CREATE OR REPLACE VIEW silver.v_nifty50 AS
    SELECT trade_date, open, high, low, close, points_chg, pct_chg, volume, turnover_cr
      FROM silver.index_daily
     WHERE index_key = 'NIFTY 50';
