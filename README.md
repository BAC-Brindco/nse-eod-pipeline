# NSE corporate-action-adjusted EOD pipeline

A production-grade, idempotent daily pipeline that ingests NSE end-of-day equity
prices, maintains a structured corporate-actions table, and materializes a
corporate-action-adjusted EOD price panel for the full NSE equity universe.

**Scope: the data layer only.** No signals, indicators, screening, dashboards,
backtests or order logic. It stops at the adjusted panel.

---

## The three-store principle

There is deliberately **no single adjusted series that gets overwritten nightly**.
A single new ex-date rescales a symbol's *entire* history, so the only way to
reconstruct any past adjusted snapshot is to keep raw bars plus dated factors:

| Store | Table | Property |
|---|---|---|
| 1. Raw as-traded bars | `bronze.eod_bhav_raw` | immutable, append-only. Source of truth for actual prices. |
| 2. Corp-action / factor table | `silver.corp_action_event` | every event, parsed ratio, computed factors, and `learned_at` — the date it was learned. |
| 3. Materialized adjusted panel | `gold.eod_adjusted` | derived nightly = raw × cumulative factor. Droppable and rebuildable. |

`rebuild-adjusted --as-of 2026-06-30` filters on `learned_at` and returns the
panel exactly as it looked then, before later-announced actions were known.

Master key is **ISIN**, never symbol — but see *ISIN canonicalization* below,
because ISINs are only stable once canonicalized.

---

## Quick start

```bash
# 1. deps (Python 3.11 via uv)
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
uv pip install --python .venv/Scripts/python.exe -e .

# 2. local Postgres (portable binaries, no admin needed)
./pg.sh start                      # starts on port 5433
./pg.sh createdb

# 3. schema
.venv/Scripts/python.exe -m nse_eod.cli migrate

# 4. verify wiring
.venv/Scripts/python.exe -m nse_eod.cli doctor

# 5. load history
.venv/Scripts/python.exe -m nse_eod.cli backfill --from 2020-01-01

# 6. daily, ~19:00 IST
.venv/Scripts/python.exe -m nse_eod.cli run-daily
```

Moving to your server changes **`DATABASE_URL` in `.env` and nothing else**.

## Commands

| Command | Purpose |
|---|---|
| `migrate` | apply all SQL migrations (idempotent) |
| `reparse-corp-actions` | re-run the current parser over ALL stored announcements |
| `derive-calendar` | establish historical trading days/holidays from observed data |
| `ingest-indices [--date] [--from --to] [--refetch]` | NSE all-index daily closes: **India VIX**, Nifty 50 and ~163 others |
| `ingest-eod [--date]` | fetch + upsert one day of bars into bronze |
| `ingest-corp-actions [--from --to]` | fetch, parse, upsert the factor table |
| `link-isins` | detect ISIN changes and resolve event ISINs |
| `rebuild-adjusted --all [--as-of]` | recompute factors and materialize gold |
| `run-daily [--date] [--force]` | the full daily cycle |
| `backfill --from [--to]` | resumable range ingest, then one gold rebuild |
| `review-queue list [--severity high] [--in-universe] [--reason]` | open items awaiting a human, universe members first then by ADV |
| `review-queue resolve --review-id N --action ... --confirm` | record a HUMAN-SUPPLIED resolution and rebuild that ISIN only |
| `assert-factors [--sweep]` | fail if any price-adjusting factor multiplies zero rows |
| `universe-status [--isin] [--blockers]` | why a security is in or out of the tradeable universe |
| `last-success [--max-age-hours] [--quiet]` | dead-man's switch; also publishes the heartbeat the morning brief reads |
| `doctor` | config, DB, schema and freshness check |

### Verification tools

| Tool | Purpose |
|---|---|
| `tools/audit_panel.py` | full-history audit: missing days, per-symbol gaps, price sanity, prev_close chain, factor coverage, unexplained jumps, invariants |
| `tools/crosscheck_external.py` | compares raw closes AND adjusted **returns** against Yahoo Finance, which keeps its own corporate-action data |
| `tools/census_check.py` | parser coverage audit over the real corp-action census |
| `tools/make_fixtures.py` | refresh test fixtures from live NSE |

`./finalize.sh` runs the whole post-backfill chain in dependency order:
derive-calendar -> reparse -> link-isins -> rebuild-adjusted --reset-factors ->
audit -> cross-check. Every step is idempotent.

**Run `reparse-corp-actions` after ANY parser change.** Bronze is immutable and
complete, so silver is always rebuildable from it — a parser fix that is not
reparsed applies only to future ingests and leaves history wrong. It supersedes
events the new parser no longer produces, refreshes open review rows, and resets
`factor_state` to `pending` only for events whose parse result actually changed.

Exit codes: **0** clean · **1** warnings · **2** critical findings · **3** crash.

---

## Verified NSE endpoints

All probed live on 2026-08-31. Nothing here is assumed.

| Purpose | URL |
|---|---|
| UDiFF bhavcopy (primary) | `nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` |
| Legacy bhavcopy (backfill) | `.../content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip` |
| Delivery + VWAP | `.../products/content/sec_bhavdata_full_{DDMMYYYY}.csv` |
| Circuit bands | `.../content/equities/sec_list_{DDMMYYYY}.csv` |
| Corporate actions | `www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={DD-MM-YYYY}&to_date={DD-MM-YYYY}` |
| Holiday master | `www.nseindia.com/api/holiday-master?type=trading` |
| Security master | `.../content/equities/EQUITY_L.csv` |

### Measured format coverage

```
legacy  cm<DDMMMYYYY>bhav.csv.zip   works <= 2024-07 ; 404 from 2024-10
UDiFF   BhavCopy_NSE_CM_...          works >= 2024-01-02 ; 404 for 2023
delivery sec_bhavdata_full           works >= 2020 ; 404 for 2019
```

Jan–Jul 2024 is served in **both** bhavcopy formats. `tests/test_parsers_bhav.py`
uses that overlap to cross-validate the two parsers against each other on 400
shared securities — the strongest available correctness evidence for the backfill.

### Scraping notes

* `nsearchives.*` needs **no cookies** — just a browser `User-Agent` + `Referer`.
* `www.nseindia.com/api/*` needs a warmed cookie jar. **The homepage returns 403
  while still setting the cookie**, so a 403 during warm-up is success, not
  failure, and must not abort the run.
* `Accept-Encoding` advertises only `gzip, deflate`. Including `br` makes NSE
  return Brotli, which httpx cannot inflate without an optional package — the
  body then arrives as raw compressed bytes and JSON decoding fails.
* The corp-action API is windowed to 60 days. It answers **HTTP 200 with a
  nonsense body** for a malformed request (e.g. `MM-DD-YYYY` instead of
  `DD-MM-YYYY`), so the response shape is validated before use — otherwise a
  wrong-format request looks exactly like "no corporate actions".

### Two-source join is unavoidable

UDiFF has ISIN but **no VWAP and no delivery**; `sec_bhavdata_full` has both but
**no ISIN**. Bronze = UDiFF (authoritative, ISIN-keyed) LEFT JOIN delivery on
`(symbol, series)` LEFT JOIN bands on `(symbol, series)`.

Column traps, verified on 20MICRONS / 2026-08-28:
* UDiFF `ClsPric` is the **close** (222.00); `LastPric` is the **last trade**
  (220.30). The legacy file lists them in the opposite order.
* UDiFF `TtlTrfVal` is **rupees**; delivery's `TURNOVER_LACS` is **lakhs**.
  Bronze stores rupees.

---

## Adjustment logic

`P` = close on the last trading day **strictly before** `ex_date`. Factors apply
to all bars strictly before the ex-date; volume moves inversely for share-count
events.

| Type | `factor_price` | `factor_vol` |
|---|---|---|
| `BONUS` num:den | `den / (den + num)` | reciprocal |
| `SPLIT` FV_old→FV_new | `FV_new / FV_old` | reciprocal |
| `SPECIAL_DIVIDEND` / `RETURN_OF_CAPITAL` | `(P − C) / P` | 1 |
| `RIGHTS` N=num/den, S=FV+premium | `(1 + N·(S/P)) / (1 + N)` | 1 |
| `RIGHTS` where `S > P` | 1.0, state `skipped_s_gt_p` | 1 |
| `DIVIDEND` / `INTERIM_DIVIDEND` | **1.0** — price series untouched | 1 |
| `AGM`/`EGM`/`INTEREST_PAYMENT`/`BUYBACK` | 1.0, `not_applicable` | 1 |
| `DEMERGER`/`SPINOFF`/`CAPITAL_REDUCTION` | 1.0 + high-severity review | 1 |

Bonus and split reduce to the same primitive, `shares_before / shares_after`, so
both call one function and cannot drift apart.

`cum_factor(isin, d)` = Π `factor_price` over events with `ex_date > d`, computed
as a **reverse** cumulative product. Two consequences worth knowing:

* **Right-edge identity** — the latest bar of every symbol has `cum_factor` exactly
  1.0, so today's adjusted close equals the exchange-printed close.
* **No look-ahead** — events with `ex_date` beyond the panel's last bar are
  excluded. Folding a future ex-date in would rescale history using knowledge of
  an event that has not happened yet.

The `*_tr` columns are a parallel **total-return** series: the same price factors
*plus* every dividend (ordinary included) reinvested at ex-date. Benchmarking
only — never use it for signal generation.

Precision: `numeric` in Postgres, `float64` in the panel, rounding only on display.

---

## What the real corporate-action text looks like

Designed against a census of **8,543 real rows** (2023-01-01 → 2026-08-31) →
1,583 distinct subjects → **325 templates**, committed as test fixtures.

Current coverage: **65 of 8,543 rows flagged (0.8 %)**, of which 47 are demergers
that genuinely need an external price. Run `tools/census_check.py` after any
parser change to see the exact effect.

Four findings that shaped the parser:

**1. NSE prints the rights *premium*, not the subscription price.**
`Rights 1:1 @ Premium Rs 11.25/-` with `faceVal=5` means **S = 16.25**. Ten of the
147 real rights rows read `@ Premium Rs 0/-` — issued *at par*, S = face value.
Using the premium as S turns the factor into `1/(1+N)`: a fabricated 50 % crash.

**2. The NCRPS trap.** `Bonus Ncrps 4:1` is a bonus of *non-convertible
redeemable preference shares* — the equity count is unchanged. A naive
`Bonus (\d+):(\d+)` reads it as a 4:1 equity bonus and destroys 80 % of that
symbol's price history. Matched and excluded *before* the bonus rule.

**3. One purpose string can carry several events.** Joined by `/`, `&`, or nothing
at all, plus NSE typos (`Annual General Meetingdividend`, 9 rows). One raw row
fans out to N events with a `component_ix`.

**4. Recognised-but-not-adjusting must not flood the queue.** `Annual General
Meeting` alone is 1,550 rows and `Interest Payment` 311. Typed explicitly as
`not_applicable`, because a 2,000-row queue is a queue nobody reads.

Nothing is ever silently dropped: every component is either a typed event or a
review-queue row (usually both, when a compound purpose parses only partially).
`silver.corp_action_override` is the path back from the queue into the factor
table — a human decision always wins over the parser.

---

## ISIN canonicalization

**A face-value split changes the ISIN.** Measured on live data (2026-08):

```
symbol      prior day    old ISIN       switch date  new ISIN       prev_close gap
TDPOWERSYS  2026-08-21   INE419M01027   2026-08-24   INE419M01035   0.0000%
KIRLPNU     2026-08-17   INE811A01020   2026-08-18   INE811A01038   0.0000%
TEMBO       2026-08-04   INE869Y01010   2026-08-05   INE869Y01028   0.0000%
CORDELIA    2026-08-24   INE0LZF01013   2026-08-25   INE0LZF01039   0.0000%
```

In every case the switch date **is** the split ex-date, and the new bar's
`prev_close` equals the old bar's close exactly — NSE preserves price continuity
even though the identifier changes.

Before this was handled, **22 of 60 price-adjusting events (37 %) referenced an
ISIN with no bars at all**, so the split factor attached to nothing and the fake
≈−50 % ex-date gap survived into the "adjusted" panel; each affected symbol also
appeared as two disconnected short histories.

`silver.isin_link` maps every ISIN ever seen to the entity's current one.
`gold.eod_adjusted.isin` is the **canonical** ISIN; `isin_traded` keeps the
as-traded identifier for provenance.

---

## Universe

`in_universe` is driven by `UNIVERSE_SERIES` in `.env`, default `["EQ","SM","ST"]`
— the main board **plus the SME platform**.

| Series | In universe | Why |
|---|---|---|
| `EQ` | ✅ | main board |
| `SM` / `ST` | ✅ | SME platform (ST is SME trade-for-trade) |
| `BE` / `BZ` | ❌ | main-board trade-for-trade *surveillance* settlement — a name lands here temporarily when NSE restricts it, so it is not freely tradeable. Its bars **are** still in gold, so a symbol's price series has no hole while restricted. |
| `INF*` ISINs | ❌ | ETF / mutual-fund **units**, not shares. They trade in series `EQ`, so series alone cannot exclude them, and NSE does not publish their unit splits in the equity corp-action feed. |
| `GS`/`GB`/`SG`/`N*`/`IV`/`RR` | ❌ | government securities, debt, InvIT/REIT units — excluded from gold entirely |

Narrow it with `UNIVERSE_SERIES=["EQ"]`; no code change needed.

## Validation

| Check | Rule | Severity |
|---|---|---|
| `prev_close_reconcile` | NSE `prev_close` vs our stored prior close, **unadjusted** | critical |
| `overnight_jump` | \|Δ\| > 20 % in the *adjusted* series with no event on file | critical |
| `rowcount_deviation` | outside trailing-20d mean ± 4σ, or below an absolute floor | critical |
| `stale_factor` | past ex-date, no factor, **and an anchor bar was available** | critical |
| `stale_factor` | past ex-date, no factor, no bar before it (window gap) | info |
| `right_edge_identity` | latest bar must have `cum_factor` = 1.0 | critical |
| `unparsed_purpose` | open high-severity review items | warning |
| `missing_symbol` | in universe yesterday, absent today, not delisted | warning |

**On `prev_close`:** NSE does **not** corporate-action-adjust it. GOODLUCK's
bonus ex-date carries `prev_close` 1439.40, the raw pre-bonus close. So this is a
*data-integrity* check (missing bar, restatement, mis-linked ISIN), not a
corporate-action cross-check — comparing against a factor-adjusted prior close
would fire on every genuine action. The corporate-action cross-check is
`overnight_jump`.

Alerts POST to `ALERT_WEBHOOK_URL` (Slack-compatible) **and** set a non-zero exit
code. With no webhook configured, findings still persist to
`ops.validation_finding` and the exit code is still set. Delivery failures never
raise — a webhook outage must not turn a successful ingest into a failed run.

---

## Idempotency

Re-running any day is a no-op, resting on four things:

1. natural-key upserts everywhere,
2. **content hashing** — an unchanged source file writes zero rows,
3. `ops.run_watermark` — a completed day is skipped,
4. gold being a pure function of bronze + the factor table.

The one legitimate exception is an **NSE restatement**: the content hash changes,
work re-triggers, and the run summary reports `bhav_rows_restated`.

---

## Testing

```bash
.venv/Scripts/python.exe -m pytest                          # all 252
NSE_EOD_SKIP_DB=1 .venv/Scripts/python.exe -m pytest -m "not integration"   # 212, offline
.venv/Scripts/python.exe tools/census_check.py              # parser coverage audit
.venv/Scripts/python.exe tools/make_fixtures.py             # refresh fixtures (network)
```

Unit tests use no network and no DB; all fixtures are real NSE files on disk.
Integration tests skip automatically when no Postgres is reachable.

Highlights: hand-checked formula cases (1:1 bonus halves price and doubles
volume; 1:2 split; rights; special dividend; S>P skipped); the whole-census
parser regression; cross-format parser equivalence on the 2024 overlap; the
golden no-fake-gap test on GOODLUCK (bonus 2:1) and TDPOWERSYS (split, across an
ISIN change); `run-daily` twice byte-identical; and gold rebuilt from bronze +
factors alone reproducing an identical fingerprint.

---

## Layout

```
migrations/         001 bronze · 002 silver · 003 gold · 004 ops
                    005 isin_linkage · 006 review_queue_dedupe
src/nse_eod/
  config.py         env/.env settings; no secrets in code
  logging_setup.py  structlog JSON + RunSummary
  db.py             psycopg3 pool, upsert helpers, run ledger, watermarks
  cli.py            typer entrypoints
  sources/          http (cookies/retry/rate-limit), bhavcopy, corp_actions,
                    holidays, security_master
  parsers/          detect, bhav (UDiFF + legacy -> one schema),
                    corp_action_text  <- the free-text parser
  transform/        factors (pure formulas), anchors, cumulative,
                    isin_link, adjusted
  validate/         checks, alerts
  orchestrate/      ingest, daily (run-daily + backfill)
tests/fixtures/     real NSE files + the 8,543-row corp-action census
tools/              census_check.py, make_fixtures.py
```

## Completeness: the calendar must never outrank the data

Both completeness bugs found in this pipeline came from trusting a calendar rule
over the published bhavcopy. Worth internalising before changing this code.

**1. NSE holds weekend sessions — Saturdays AND Sundays.** A `weekday() >= 5`
skip lost eight real trading days over 2020-2026:

| Date | Day | Session | Bars |
|---|---|---|---|
| 2020-02-01 | Sat | Budget | 1,886 |
| 2020-11-14 | Sat | Diwali Muhurat | 1,878 |
| **2023-11-12** | **Sun** | Diwali Muhurat | 2,463 |
| 2024-01-20 | Sat | special live session | 2,590 |
| 2024-03-02 | Sat | special live session | 2,440 |
| 2024-05-18 | Sat | special live session | 2,512 |
| 2025-02-01 | Sat | Budget | 2,866 |
| **2026-02-01** | **Sun** | Union Budget | 3,229 |

`never_trades()` now returns **`False` unconditionally**. It returned `True` for
Sunday until 2026-02-01 falsified that. Every calendar assumption made while
building this pipeline was eventually wrong, and each cost a real trading day —
so only a 404 (0.16 s) may rule a day out. `is_trading_day` likewise refuses to
let a `weekend_rule` calendar row veto a weekend day; only the published holiday
master or an observed bhavcopy can.

Note the `missing_trading_days` audit scans **weekdays only**, so a missed
weekend session is invisible to it — hence the separate `weekend_sessions` check.

**2. A 2-digit year cost a whole trading day.** NSE wrote `13-Jul-20` in the
2020-07-13 legacy bhavcopy while every other day writes `14-JUL-2020`. `%d-%b-%Y`
parses `"20"` as **year 20 AD**, so the frame came back dated `0020-07-13`, the
expected-date check rejected the file, and the day was marked `failed` with no
parse error to point at. `_flexible_dmy` normalises the year textually first.

**How it was found**, which is the reusable part: the `prev_close` chain showed
breaks of *almost exactly 20.0%* — the circuit limit. A one-circuit gap means a
day is missing between two bars and `prev_close` is correctly pointing at it.
CLNINDIA's absent 2020-07-13 closed at +20.0%; SHAKTIPUMP's absent Budget
Saturday closed at +20.0%.

**3. The holiday API only covers the current year.** It returned 20 rows for 2026
and nothing for 2020-2025, so 80+ weekdays had no calendar entry and an audit
could not tell a holiday from a missing ingest. `derive-calendar` settles it from
evidence: a weekday with bars traded; a weekday whose fetch was attempted and
found nothing did not. A date never attempted stays absent, so a real coverage
gap remains visible.

**4. A 404 is an answer, not a failure.** `NSENotFound` subclasses
`NSEFetchError`, so the retry predicate retried every 404 five times with
exponential backoff — **33 s per holiday**, which made probing a multi-year range
for Saturday sessions unusably slow. `retry_if_not_exception_type(NSENotFound)`
took it to 0.16 s.

## Corporate-action feed coverage

**The CA API is segmented by `index`, and `equities` EXCLUDES the SME platform.**
`CA_INDICES=["equities","sme"]`. Fetching only `equities` left ~1,350 SME stocks
with invisible splits and bonuses, surfacing as unexplained −80% to −96%
single-day drops (ISHAN split ÷10 + bonus 2:1 = ÷30; GOLDSTAR; SECL; CELLECOR;
COOLCAPS; VMARCIND bonus 5:1; MOS; JSLL). The SME feed added 1,044 raw rows and
**+206 price-adjusting events**. Its purpose text arrives in UPPERCASE, which the
case-insensitive parser handles. Other segments exist (`debt`, `sse`,
`municipalBond`, `invitsreits`) and are deliberately not fetched.

## The silent-failure class: orphaned factors

The most dangerous bug found. HDFC Bank's 2025 `Bonus 1:1` parsed, computed
`factor_price = 0.500000`, and reached `factor_state = 'computed'` — every
component healthy. But NSE filed it under `INE040A01018`, the **pre-merger**
ISIN, while every bar sits under `INE040A01034`. The factor multiplied nothing,
and India's largest private bank carried its whole pre-August-2025 history
unadjusted by 2×.

No existing check caught it: `right_edge_identity` passed (cum_factor was
consistently 1.0), `stale_factor` passed (the factor existed). Only
`unexplained_jumps` noticed, as a −50.4% move.

`tools/audit_panel.py` now has an **`orphaned_factors`** check expressing that
invariant directly: a computed, non-unit factor whose ISIN has no earlier bars in
the panel. It separates three cases — wrong ISIN (a defect), event predates the
security's first bar (fine), and instrument excluded from the panel by design
(InvIT/REIT units).

## ISIN linkage: series is an attribute of a bar, never an identity

Indian securities migrate between series constantly (SRPL went SM → EQ → BE → BZ),
often *around* the very corporate action that changes their ISIN. Chaining per
`(symbol, series)` broke three separate things: the right-edge check flagged 20
false violations, VERTOZ's consolidation orphaned a third ISIN, and ad-hoc
`prev_close` queries reported thousands of phantom breaks. Anything reasoning
about a security's timeline must collapse to one row per (symbol, day) first.

Linkage signals, strongest first:

1. **`prev_close_continuity`** — adjacent bars, ISIN changed, new bar's
   `prev_close` equals the old close. Matched to 0.0000% in every observed case.
2. **`symbol_match`** (medium) — for switches `prev_close` cannot vouch for.
   VERTOZ was *suspended* 2025-06-25→07-10 across its consolidation, so NSE
   reported a reference `prev_close` (9.81) rather than the last close (9.17).
   Requires the same symbol, the same 9-character ISIN stem, and the new ISIN
   starting within 120 days.

Two guards learned the hard way: the ISIN **type digits are at positions 8–9**
(`01` equity, `07` debt, `13` warrant) — getting that offset wrong silently
matched nothing — and both series and type must be filtered, or every NCD tranche
of one issuer merges (DHANILOANS, DHFL) and warrants register as ISIN switches
(11,574 spurious candidates). Chains are re-flattened transitively after the
second pass.

Span lookups are keyed on the **canonical ISIN, not (canonical, symbol)**: a
symbol rename otherwise fragments the timeline. URAVI became URAVIDEF in 2025 and
NSE filed its 2022 bonus under the *new* symbol.

## Scheduling the nightly run

`run_daily.bat` is the entry point; `scheduler/` holds the registration artifacts.

```bat
scheduler\register_task.cmd          :: schedule only  (schtasks CLI)
schtasks /Create /TN "NSE EOD Daily" /XML scheduler\nse_eod_daily.xml /RU "DOMAIN\user" /RP
```

Register from an **elevated** prompt. `/RU` and `/RP` are supplied interactively;
no password is stored in the repo or in the XML (`<UserId>` is a placeholder that
`/RU` overrides).

### Exit-code translation, and why it exists

`run-daily` exits **0** clean, **1** warnings, **2** critical, **3** crash. Task
Scheduler treats *any* non-zero result as a failure, so `run_daily.bat` translates
**1 to 0** and propagates only `>= 2`.

Without that translation the task retries every single night, because open review
items are the normal steady state (208 of them today) - and a retry signal that
fires nightly is indistinguishable from no signal at all.

Verified in both directions: raw 1 -> batch 0, raw 2 -> batch 2.

> **The `endlocal` trap.** The last line is `endlocal & exit /b %TRC%` with
> `%TRC%`, **not** `!TRC!`. In a compound `endlocal & exit /b`, the delayed form is
> expanded *after* `endlocal` has already discarded the variable, so it expands to
> nothing and `exit /b` returns **0**. The observed symptom was the log correctly
> printing `propagating raw=2` while the caller received 0 - a real failure
> reported as success, with no retry. `%TRC%` is expanded at parse time.

### Retry

`schtasks.exe` **cannot** set retry-on-failure - the CLI form gives the schedule
only. Import `scheduler/nse_eod_daily.xml` for `RestartOnFailure` (`PT15M` x 3,
`ExecutionTimeLimit PT2H`). Confirmed present after registration.

The XML must be **UTF-16** because it declares `encoding="UTF-16"`; `schtasks
/Create /XML` rejects a mismatch. Note that PowerShell's `[xml]` cast parses a
*string*, so casting the file's text validates nothing about its encoding - this
latent failure was masked exactly that way.

### TIMEZONE - check before every deployment

`/ST` and `<StartBoundary>` are in **server local time**, not UTC and not IST.
Verified on this host:

```powershell
Get-TimeZone | Select-Object Id,BaseUtcOffset
# Id = "India Standard Time", BaseUtcOffset = 05:30:00
```

Local time *is* IST here, so `19:00` is correct as written. **On a UTC-clock
server, 19:00 IST is `13:30` UTC** - change `/ST` to `13:30` and
`<StartBoundary>` to `T13:30:00`.

Getting this wrong is silent, and worse than an outright failure: at 13:30 IST the
bhavcopy has not been published, so the run finds no data, skips cleanly, and the
dead-man's switch below still looks satisfied. `register_task.cmd` prints the time
zone and pauses before registering for this reason.

### Drive mapping

`Z:` on this host is a **local fixed volume** (`Win32_LogicalDisk` DriveType 3,
label "Storage"), not a mapped network drive - `net use` lists no `Z:` mapping. It
is therefore visible to a scheduled task running off-session and needs no UNC
indirection.

The "mapped drives are invisible to service accounts" caveat applies only to
drives created with `net use` inside an interactive logon session: those live in
that session's namespace, so a task running as SYSTEM or with "run whether user is
logged on or not" sees no `Z:` at all and the batch file fails at the first
`pushd`. If this repo moves to a real share, use the commented UNC form already in
`run_daily.bat`:

```bat
set "REPO=\\SERVERNAME\quant\nse_eod_pipeline"
pushd "%REPO%" || (echo [FATAL] cannot reach %REPO% & exit /b 3)
```

`pushd` on a UNC path allocates a temporary drive letter for the life of the
script, which is the only reliable way to reach a share from a scheduled task.

### The dead-man's switch

A silent no-run is the dangerous failure: signals fire on stale prices and nothing
looks broken. `last-success` reads `ops.v_last_success` and exits **2** when the
last completed run is older than `--max-age-hours` (default 30h - a normal weekday
gap plus slack; a Monday morning legitimately sees ~65h).

`run_daily.bat` calls it after every run so the answer lands in the day's log, and
it publishes `HEARTBEAT_PATH` (default `data/heartbeat.json`) for out-of-process
consumers.

**The morning brief consumes it.** `D:\bac_morning_brief` reads that stamp
(`src/bac_brief/qc/eod_freshness.py`) and prints `EOD pipeline last ran: <ts>` in
the masthead and colophon, with an amber notice when the panel does not cover the
session the brief describes. So a no-run is caught at ~07:30 by a human reading
the brief, rather than at signal time.

Two deliberate choices there:

* **The brief reads a file, not this database.** It needs no DSN, no driver and no
  network path to this host - which matters because the case that must be reported
  loudest is the one where this host's environment is broken.
* **The verdict compares trade dates, not elapsed hours.** The panel is judged
  against the session the brief actually describes. An hours threshold cannot be
  right for both a Tuesday (~12h healthy) and a Monday (~60h healthy), and a
  warning that fires every Monday gets muted.

The stamp's contents are DB-derived, never wall-clock, so running `last-success`
by hand rewrites the file *without* moving `last_success_at` forward. Consumers
must read `last_success_at` and never the file's mtime - otherwise the reporting
command would forge the signal it reports on.

## Index closes and India VIX

The equity bhavcopy carries no index rows, so the pipeline had neither an index level
nor India VIX. Both come from one archive file:

```
https://nsearchives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv
```

Verified live 2026-09-03: 200 for every trading day probed from 2018-06-29 to
2026-09-01, ~165 index rows per file, no cookie required (nsearchives never needs
one). Weekends and holidays 404 -- the same non-trading-day signal the equity path
uses, so absence is information rather than a gap.

```
India VIX,01-09-2026,11.19,12.1225,9.2475,11.49,0.3,2.7,-,-,-,-,-
Nifty 50,01-09-2026,24077.55,24143.15,23952.55,24055.8,-24.6,-.1,334207690,...
```

`bronze.index_close_raw` (text verbatim) -> `silver.index_daily` (typed), plus
`silver.v_india_vix` and `silver.v_nifty50` for convenience. Ingested by
`ingest-indices`, and by `run-daily` as **step 3b**.

**Step 3b is deliberately non-fatal.** The equity panel is this pipeline's product
and must not fail to build because a secondary index file was published late. A
missing day becomes a warning; the next run picks it up, because `ingest_range`
skips dates already stored.

### Two endpoints that do NOT work

Recorded so nobody spends the probing time again:

| Endpoint | Result |
|---|---|
| `/api/historical/vixhistory` | **503** after full retry/backoff. Dead. |
| `/api/allIndices` | 200 and it does carry `INDIA VIX` -- but it is an **intraday snapshot** (observed timestamp 09:50 mid-session). Using it for a daily series would record a mid-session print as the close. |

Also not usable: the `INDIAVIX` rows in `fo_bronze` are `FUTIVX` -- VIX **futures**,
from a product NSE delisted in 2015 (coverage 2014-02 to 2015-04, contract level). A
futures price is not the index level.

### The "-" trap

India VIX prints `-` for volume, turnover, P/E, P/B and dividend yield, because a
volatility index has none of those. Parsing `-` as `0` would put a real number where
there is no measurement, and a downstream liquidity filter would believe it. Every
such field is NULL, and `tests/test_indices.py` pins it.

The parser also **validates the header** and **refuses a file whose own date column
disagrees with the date requested** -- NSE has changed layouts before (the equity
bhavcopy moved to UDiFF mid-2024), and positional parsing against a reordered header
would write highs into the low column without erroring.

## Known gaps

* **Demergers/spin-offs (47 in the census) cannot be computed.** Bloomberg's
  `1 − (B·N)/P` is implemented, but no NSE EOD product publishes the spun-off
  entity's close (`B`). They are typed, given factor 1.0, and flagged
  high-severity for resolution via `corp_action_override`.
* **ETF/mutual-fund unit splits are absent from the equity corp-action feed.**
  `INF*` ISINs are classified as `ETF_MF` and excluded from the universe; an
  unexplained ISIN switch is queued with an implied ratio that is **never**
  auto-applied.
* **Delivery and VWAP are NULL before 2020**, which is why `backfill_start`
  defaults to 2020-01-01.
* Sector is not populated — `EQUITY_L.csv` does not carry it.
