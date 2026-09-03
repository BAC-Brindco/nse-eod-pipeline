# NSE CA-Adjusted EOD Pipeline — Design Proposal

> **Status: BUILT and superseded by [README.md](README.md).**
> This file is the pre-implementation proposal, kept for the endpoint recon and
> the corp-action census that justified the design. Four things were discovered
> only once the code ran against live data, and are documented in the README:
>
> 1. **A face-value split changes the ISIN.** 22 of 60 price-adjusting events
>    referenced an ISIN with no bars, so split factors attached to nothing.
>    Added `silver.isin_link` + migration 005.
> 2. **NSE does not corporate-action-adjust `prev_close`.** The reconciliation
>    check in §6 below had it backwards and would have fired on every genuine
>    corporate action. It is now a data-integrity check.
> 3. **Future-dated ex-dates must be excluded** from `cum_factor`, or history is
>    rescaled using knowledge of an event that has not happened (look-ahead).
> 4. **`UNIQUE (ca_raw_id, raw_purpose)` does not dedupe NULL `ca_raw_id`**, so
>    the review queue grew every run. Fixed by an expression index (migration 006).
>
> Also: ETF/mutual-fund units (`INF*` ISINs) trade in series `EQ` but their unit
> splits are absent from the equity corp-action feed, so they are excluded from
> the universe.

Status: **awaiting sign-off before implementation**
Root: `Z:\nse_eod_pipeline`
Recon date: 2026-08-31 (all endpoints below were probed live, not assumed)

---

## 1. Verified NSE endpoints

All confirmed with live HTTP probes today. Status codes and byte counts observed.

| # | Purpose | URL | Verified |
|---|---------|-----|----------|
| 1 | **UDiFF bhavcopy** (primary, live) | `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | 200, ~202 KB zip, 3 612 rows |
| 2 | **Legacy bhavcopy** (backfill only) | `https://nsearchives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip` | 200 for 2019/2024-07; **404 from 2024-10 onward** |
| 3 | **Delivery + VWAP** | `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv` | 200, 3 460 rows |
| 4 | **Price bands** (circuit) | `https://nsearchives.nseindia.com/content/equities/sec_list_{DDMMYYYY}.csv` | 200, `Symbol,Series,Security Name,Band,Remarks` |
| 5 | **Corporate actions** | `https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={DD-MM-YYYY}&to_date={DD-MM-YYYY}` | 200 JSON; **8 543 rows harvested for 2023-01-01 → 2026-08-31** |
| 6 | **Holiday master** | `https://www.nseindia.com/api/holiday-master?type=trading` | 200 JSON, segment keys incl. `CM` |
| 7 | **Security master** | `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | 200, 181 KB |

### Format coverage boundaries (measured)

```
legacy  cm<DDMMMYYYY>bhav.csv.zip   ........ works <= 2024-07 ; 404 from 2024-10
UDiFF   BhavCopy_NSE_CM_...          ........ works >= 2024-01-02 ; 404 for 2023
sec_bhavdata_full (delivery/VWAP)    ........ works >= 2020 ; 404 for 2019
```

There is a **Jan–Jul 2024 overlap where both formats resolve** — the backfill will
cross-validate the two parsers against each other over that window as a free
correctness test.

### Scraping behaviour observed

- `nsearchives.nseindia.com` (files 1–4, 7) served **without any cookie**; only a
  browser `User-Agent` + `Referer: https://www.nseindia.com/all-reports` needed.
- `www.nseindia.com/api/*` (files 5–6) **requires cookie warm-up**. The homepage
  itself returned **403** on first hit yet still set `AKA_A2`; a second GET of
  `/companies-listing/corporate-filings-actions` raised the jar to 6 cookies and
  the API then returned 200. So: warm two pages, tolerate a 403 on the homepage,
  and re-warm on any 401/403.
- The CA API caps a request window; 60-day windows returned reliably (max 1 039 rows).

### Two-source join is unavoidable

| Field | UDiFF | sec_bhavdata_full |
|---|---|---|
| ISIN | ✅ authoritative | ❌ absent |
| VWAP (`AVG_PRICE`) | ❌ absent | ✅ |
| `DELIV_QTY` / `DELIV_PER` | ❌ absent | ✅ |

Bronze ingest = UDiFF (ISIN-keyed, authoritative OHLCV) **LEFT JOIN**
`sec_bhavdata_full` on `(symbol, series)` for `vwap`, `deliv_qty`, `deliv_pct`,
**LEFT JOIN** `sec_list` on `(symbol, series)` for `price_band`.
Delivery data is nullable by design — it does not exist before 2020.

Column mapping note: UDiFF `ClsPric` is the **close**, `LastPric` is the **last
trade**. These are *not* the same and the legacy file names them in the opposite
order (`CLOSE`, `LAST`). Verified on 20MICRONS 2026-08-28: UDiFF `ClsPric=222.00`,
`LastPric=220.30`; sec_bhavdata `CLOSE_PRICE=222.00`, `LAST_PRICE=220.30`. Consistent.
Turnover units differ: UDiFF `TtlTrfVal` is **rupees**, sec_bhavdata `TURNOVER_LACS`
is **lakhs**. Bronze stores rupees.

---

## 2. What the real corp-action text actually looks like

Harvested **8 543 rows** (2023-01-01 → 2026-08-31): 1 583 distinct `subject`
strings collapsing to **325 templates**. Fixtures committed at
`tests/fixtures/corp_actions_real_2023_2026.json` and `subject_templates.txt`.
The parser is written against this census, not against imagination.

Series mix: `EQ` 7 959, `GS` 313, `IV` 215 (InvIT), `RR` 56.

### Findings that change the design

**(a) NSE prints the rights PREMIUM, not the subscription price.**
`Rights 1:1 @ Premium Rs 11.25/-` with `faceVal=5` means **S = 5 + 11.25 = 16.25**.
And `Rights 1:1 @ Premium Rs 0/-` (`faceVal=5`) is an issue **at par, S = 5** — not
S = 0. Taking the premium as S would produce a wildly wrong factor and, for the
`Premium Rs 0/-` cases, a factor of `1/(1+N)` — a fake 50 % crash. This is why the
API ships `faceVal` on every row, and why `face_value` is carried into the factor table.

**(b) The NCRPS trap.** 5 rows read `Bonus Ncrps 1:116` / `Scheme Of Arrangement -
Bonus Ncrps 4:1`. These are bonuses of **non-convertible redeemable preference
shares** — the equity count is unchanged, so the equity price must **not** be
rescaled. A naive `Bonus (\d+):(\d+)` regex reads `Bonus Ncrps 4:1` as a 4:1 equity
bonus and destroys 80 % of that symbol's price history. `BONUS_PREFERENCE` is
matched and excluded **before** the bonus rule ever runs.

**(c) One row can carry several events.** Compound purposes joined by `/`, `&`, or
nothing at all:
```
Annual General Meeting/Dividend - Rs 5 Per Share/Special Dividend - Rs 10 Per Share
Interim Dividend - Rs 8 Per Share Special Dividend - Rs 67 Per Share      <- no separator
Annual General Meetingdividend - Rs 6 Per Share                           <- NSE typo, 9 rows
```
So `corp_action_raw` (1 row) → `corp_action_event` (N rows), each with a
`component_ix`. The parser splits, then classifies each component independently. A
purpose where *some* components parse and others do not yields the parsed events
**plus** a `multi_component_partial` review row — never a silent partial.

**(d) `Re` vs `Rs`.** ~1 300 rows use `Re` for amounts below 1 (`Dividend - Re 0.40
Per Share`). Also `Per Sh` (90 rows), `Rs# Per Share` (no space), `Rs  6` (double
space), `Rs 09` (leading zero). All in the token regex.

**(e) Recognised-but-not-adjusting must not flood the queue.** `Annual General
Meeting` alone is **1 550 rows** and `Interest Payment` 311. Classified explicitly as
`AGM` / `INTEREST_PAYMENT` / `BUYBACK` with `factor_state='not_applicable'`. If these
went to review, the queue would be ~2 000 rows of noise and nobody would ever read
it. The queue must stay small enough to actually be worked.

**(f) Needs external data → high-severity review, factor stays 1.0.** `Demerger`
(47), `Capital Reduction` (2), `Redemption` (2) require the spun-off entity's price,
which no NSE EOD feed provides. Bloomberg's spin-off formula `1 - (B*N)/P` is
implemented and ready, but `B` is unavailable, so these are typed, given
`factor_price = 1.0`, and flagged `needs_external_price` at **high** severity —
visible and resolvable via `corp_action_override`, never guessed.

**(g) REIT/InvIT distributions are a long tail of ~120 near-unique phrasings.**
```
Distribution - Rs 1.6876 Per Unit Consists Of Dividend Re 0.2958 Per Unit/
  Interest - Re 0.7046 Per Unit/ Return Of Capital - Re 0.36 Per Unit
```
The `Return Of Capital` / `Capital Repayment` / `Repayment Of Spv Debt` components
**are** price-adjusting per Bloomberg. A component extractor pulls the labelled
amounts; anything whose components do not sum to the stated headline total within
1 paisa goes to review rather than being applied. These are `series IV`, flagged
non-`EQ`, and out of `in_universe` — so they cannot contaminate the equity panel.

---

## 3. Module layout

```
Z:\nse_eod_pipeline\
├── pyproject.toml                  # py>=3.11, polars, psycopg[binary,pool], typer,
│                                   # httpx, tenacity, python-dotenv, structlog, pytest
├── .env.example                    # DB URL, DATA_DIR, ALERT_WEBHOOK_URL, rate limits
├── README.md                       # runbook
├── PROPOSAL.md                     # this file
├── migrations\
│   ├── 001_bronze.sql              # ✅ written
│   ├── 002_silver.sql              # ✅ written
│   ├── 003_gold.sql                # ✅ written
│   └── 004_ops.sql                 # ✅ written
├── src\nse_eod\
│   ├── config.py                   # pydantic-settings; fails fast on missing DB URL
│   ├── logging_setup.py            # structlog JSON, run_uid on every line, RunSummary
│   ├── db.py                       # psycopg3 pool, COPY-based bulk upsert, run ledger
│   ├── cli.py                      # typer: ingest-eod ingest-corp-actions
│   │                               #        rebuild-adjusted run-daily backfill
│   │                               #        + migrate, review-queue, doctor
│   ├── sources\
│   │   ├── http.py                 # NSESession: cookie warm-up, tenacity backoff,
│   │   │                           # token-bucket rate limit, on-disk raw archive
│   │   ├── bhavcopy.py             # UDiFF + legacy fetch, format routing by date
│   │   ├── delivery.py             # sec_bhavdata_full
│   │   ├── pricebands.py           # sec_list_<DDMMYYYY>.csv
│   │   ├── corp_actions.py         # CA API, 60-day windowing
│   │   ├── holidays.py             # holiday-master -> trading_calendar
│   │   └── security_master.py      # EQUITY_L.csv
│   ├── parsers\
│   │   ├── detect.py               # sniff udiff vs legacy from the header row
│   │   ├── udiff.py                # -> canonical polars frame
│   │   ├── legacy.py               # -> same canonical frame
│   │   └── corp_action_text.py     # ★ the free-text parser (see §2)
│   ├── transform\
│   │   ├── factors.py              # pure formula functions, no I/O
│   │   ├── anchors.py              # resolve P = close on last trading day < ex_date
│   │   ├── cumulative.py           # cum_factor per (isin, date), as-of aware
│   │   └── adjusted.py             # materialize gold, per-ISIN rebuild
│   ├── validate\
│   │   ├── checks.py               # prev_close recon, 20% jump, rowcount, missing, stale
│   │   └── alerts.py               # Slack-compatible webhook + exit code
│   └── orchestrate\
│       ├── daily.py                # run-daily state machine
│       └── backfill.py             # date-range driver, resumable
└── tests\
    ├── fixtures\
    │   ├── corp_actions_real_2023_2026.json   # ✅ 8 543 real rows
    │   ├── subject_templates.txt              # ✅ 325 templates w/ counts
    │   ├── udiff_sample.csv                   # to snapshot
    │   └── legacy_sample.csv                  # to snapshot
    ├── test_corp_action_text.py    # every template family + the NCRPS trap
    ├── test_factors.py             # hand-checked: 1:1 bonus, 1:2 split, rights, spl div, S>P
    ├── test_cumulative.py          # ordering, as-of replay
    ├── test_adjusted_golden.py     # real symbol across a split: no ex-date gap
    ├── test_parsers_bhav.py        # UDiFF/legacy -> identical canonical schema
    ├── test_idempotency.py         # run-daily twice == identical DB state
    ├── test_backfill_determinism.py# gold rebuilt from bronze+factors only
    └── test_validation.py          # each check fires on a crafted breach
```

No live network in unit tests: `NSESession` is injected, and all fixtures are on disk.

---

## 4. Adjustment logic (as specified, with the anchor resolved)

`P` = close on the **last trading day strictly before `ex_date`**, read from
`bronze.eod_bhav_raw` via `trading_calendar`. If no such bar exists (newly listed,
or a suspended symbol), the event is parked at `factor_state='needs_anchor'` and
retried on later runs — never silently defaulted to 1.0.

| Type | `factor_price` | `factor_vol` |
|---|---|---|
| `BONUS` num:den | `den / (den + num)` | reciprocal |
| `SPLIT` / `FACE_VALUE_CHANGE` FV_old→FV_new | `FV_new / FV_old` | reciprocal |
| `SPECIAL_DIVIDEND` / `RETURN_OF_CAPITAL` | `(P - C) / P` | 1 |
| `RIGHTS` N=num/den, S=FV+premium | `(1 + N*(S/P)) / (1 + N)` | 1 |
| `RIGHTS` where `S > P` | **1.0**, `factor_state='skipped_s_gt_p'` | 1 |
| `DIVIDEND` / `INTERIM_DIVIDEND` | **1.0** (price series untouched) | 1 |
| `AGM`/`EGM`/`INTEREST_PAYMENT`/`BUYBACK` | 1.0, `not_applicable` | 1 |
| `DEMERGER`/`SPINOFF` | 1.0 + high-severity review | 1 |

Bonus in NSE notation: `Bonus 1:1` = 1 new for 1 held → before 1, after 2 →
factor 0.5. `Bonus 13:10` → `10/23 = 0.43478…`.
Split `From Rs 10 To Rs 2` → factor `2/10 = 0.2`.
Note both reduce to the same primitive, `shares_before / shares_after`, so
`factors.py` exposes one `share_count_factor(before, after)` that both call.

Cumulative, per `(isin, d)`:
`cum_factor(isin, d) = Π factor_price` over non-superseded events with `ex_date > d`.
Computed as a reverse-cumulative product over the event list — so the most recent
bar always has `cum_factor = 1.0` and adjusted prices equal raw prices at the right
edge. Total-return uses the same product but additionally folds
`(P - D_total) / P` for **every** dividend, ordinary included.

Precision: `float64` in polars for the panel, `Decimal`/`numeric` in Postgres for
factors. Rounding only at display.

---

## 5. Daily orchestration and idempotency

`run-daily` (target ~19:00 IST):

1. **Calendar gate** — weekend or `trading_calendar.is_trading_day = false` → write a
   `skipped` run row, exit 0.
2. **Watermark check** — `ops.run_watermark(command, target_date)` already `success`
   **and** source `content_hash` unchanged → no-op, exit 0. A restated NSE file
   changes the hash and re-triggers work; that is the one case where a re-run is
   not a no-op, and it is logged as `bhav_rows_restated`.
3. Fetch bhavcopy → validate → upsert bronze (`ON CONFLICT (trade_date,isin,series) DO UPDATE`
   only when `row_hash` differs).
4. Fetch CA window `[today-90d, today+90d]` → append bronze → parse → upsert events →
   review queue.
5. Resolve anchors for events whose `ex_date` just became resolvable.
6. Recompute `cum_factor` **only for ISINs touched** by a new/changed event, plus
   today's bars. A new ex-date rescales that symbol's whole history, so the unit of
   rebuild is the ISIN, not the day.
7. Materialize gold for those ISINs.
8. Run validation suite → persist findings → webhook on `critical` → exit non-zero.
9. Write the run summary.

Idempotency rests on: natural-key upserts everywhere, content hashing, the
watermark table, and a gold table that is a pure function of bronze + factors.

---

## 6. Validation

| Check | Rule | Severity |
|---|---|---|
| `prev_close_reconcile` | `bhav.prev_close` vs prior bar's close × same-day ex factor, tol 0.5 % | critical |
| `overnight_jump` | \|Δ ln(c_adj)\| > 20 % with no event on that ex-date | critical |
| `rowcount_deviation` | outside trailing-20d mean ± 4σ (view `ops.v_bhav_rowcount_norm`) | critical |
| `unparsed_purpose` | any new `open` + `high` review row | warning |
| `missing_symbol` | in universe yesterday, absent today, not delisted | warning |
| `stale_factor` | event with `ex_date` past and `factor_state` still `pending`/`needs_anchor` | critical |
| `constraint_violation` | any DB integrity error | critical |

`prev_close_reconcile` is the load-bearing one: NSE's own `prev_close` already
reflects corporate actions, so comparing it against our factor-adjusted prior close
is an **independent cross-check of the factor itself**. If we missed a bonus, this
fires on the ex-date — before the bad data reaches anything downstream.

Alerts POST to `ALERT_WEBHOOK_URL` (Slack-compatible JSON) **and** set exit code 1
(`warning`) / 2 (`critical`).

---

## 7. Assumptions I am proceeding on

1. **Postgres/Supabase target, connection via `DATABASE_URL`.** Direct
   `psycopg3` + SQL migrations, no ORM — the `numeric` precision and `COPY` bulk
   paths matter more here than model ergonomics.
2. **Python 3.11 via `uv`** (`.venv` in the project root). `polars` is not installed
   on the 3.14 system interpreter, and 3.14 is ahead of some wheels; 3.11.15 is
   already present on this machine and is the safe target.
3. **`httpx` over `requests`** for the fetch layer (timeout ergonomics, HTTP/2),
   with `tenacity` for backoff. Recon used `requests` for speed only.
4. **Backfill horizon default 2020-01-01** — the earliest date where delivery data
   exists, so the panel is column-complete. `--from 2016-01-01` still works, with
   `deliv_*` NULL. Legacy parser handles pre-2024.
5. **Universe** = `series = 'EQ'` and `status != 'delisted'` as at `trade_date`.
   `BE`/`BZ`/`T2T`/`SM`/`ST` are ingested and flagged via `series_flag` but
   `in_universe = false`. `GS`/`IV`/`RR` and other non-equity are excluded from gold.
6. **Delisted names are retained forever** with `status='delisted'` and their bars
   frozen — required for honest backtests.
7. **CA scrape window ±90 days daily**, because NSE both announces ahead and revises
   after; the `learned_at` column captures when we found out.
8. `sec_list.csv` (undated) is the current-snapshot alias of
   `sec_list_<DDMMYYYY>.csv`; the dated form is used so bands are point-in-time.

---

## 8. Open questions (only these are actually blocking)

1. **`DATABASE_URL`** — Supabase project/connection string, or should I target a
   local Postgres for now and leave Supabase to config? Migrations are DDL and the
   pooled anon key cannot run DDL (a constraint this desk has hit before), so this
   needs the direct/service connection.
2. **`ALERT_WEBHOOK_URL`** — Slack webhook, or leave unset (findings still persist
   to `ops.validation_finding` and still set the exit code)?
3. **Backfill start date** — confirm 2020-01-01, or go back further and accept NULL
   delivery data?

Everything else I will proceed on under the assumptions in §7.
