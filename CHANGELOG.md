# Changelog

## 0.3.0-dev — 30 Oct 2026

Milestone 3 (in progress).

- **Instrument C: private-credit fund share** (`PCF-USD-SIM`). Its value is a NAV the manager reports, never a price: each report records the NAV per share, the date it values the portfolio at, when it was published (UTC) and its source. The dashboard shows the NAV, its age (30 days on 30 Oct), days since it was reported, and whether it is stale (over 45 days). Quoting is always blocked on a fund share, with the reason shown. The figures are invented and labelled so. Withdrawals, the secondary price and the discount come in M5.
- New table `nav_reports` (migration `002_nav_reports.sql`), append-only like curves. The configured NAV is stored once; restarts don't duplicate it or its `nav_loaded` audit event. Checked on a copy of the real main database: the migration applied once and kept all 33 existing events.
- The API's `/api/overview` has a new `funds` list; `instruments` still holds the two bonds only.
- The migration test now expects every file in `migrations/`, in order, instead of a hard-coded list.
- **Holder ledger (M3 step 2).** A double-entry journal in SQLite (migration `003_ledger.sql`): every transaction sums to zero per asset, is written all or nothing, and is idempotent by id (the same id with different contents is refused). Balances are worked out as of any date by summing entries in `Decimal`. Four fictional test holders from `config/holders.json` get their starting positions through one issuance transaction per instrument; issuers are parties whose negative balance is what they have issued. Transfers between holders are checked: no going below zero, no back-dating, no transfers with issuers. Every posting is in the audit log (`ledger_posted`).
- **Scenario database.** `python scripts/new_scenario_db.py` creates `data/scenario.sqlite3` with the seeded holders; running it again archives the previous one to `data/archive/` and starts fresh. Each database records whether it is `main` or `scenario` (set once); the script refuses to replace anything not marked scenario and doesn't modify it. The dashboard header shows which database it is reading, highlighted for a scenario. The main database is seeded on start too.
- **Coupons (M3 step 3).** `debtlab/lifecycle.py` and migration `004_payments.sql` (`payment_events`, `entitlements`). Runs only in a scenario database; anything else is refused, including a database older than the role marker (found running it on a copy of the real main database, which crashed instead of refusing; `get_db_role` now reads such a database as unmarked without changing it). The scenario starts a **simulation date** on the day it is created; it only moves forward. A coupon is **scheduled** first, fixing who is owed what from holdings at the end of the **record date** (short bond 15 calendar days before payment, century bond 1 business day before; set per bond in `config/instruments.json`), then **paid** one holder at a time, each holder's ledger transaction and *paid* mark committed together. A run only pays what is still due, so a crash part-way is finished by running again and nobody is paid twice. Dana's amounts are recorded as unpaid (compliance hold). Per-holder cash is rounded to cents; the rounding residue is stored. Scheduling closes the books: the ledger then refuses movements in that bond dated on or before the record date. All audited (`coupon_scheduled`, `ledger_posted`, `coupon_run_finished`, `simulation_advanced`).
- `python scripts/lifecycle.py status | pay | advance DATE` drives it. Live run on a fresh scenario database: the 17 Aug 2026 coupon paid Alder $90,000, Birch $67,500, Cedar $45,000, left Dana's $22,500 unpaid (status *partly paid*, $202,500 of $225,000); a second `pay` changed nothing; `advance 2027-03-30` paid the Feb 2027 short coupon and the first century coupon (£300,000, 6.00 per 100); moving back was refused.
- **Dashboard banner wording** (spotted by Ethan). In offline mode it said official sources "weren't reachable", although offline mode is chosen in the settings and contacts nothing. It now says offline mode was chosen (`DEBTLAB_DATA_MODE=offline`). Sources that actually fail are listed separately, per currency with the error and the stored curve used instead, from a new `source_failures` list in `/api/overview`.
- Migrations: a `;` inside a SQL comment no longer splits a statement (it broke the first draft of migration 003).
- **Fixes from the 30 Oct code review** (ultrareview, run by Ethan):
  - A source returning bad content (a non-numeric cell, a missing column, a corrupt workbook, a failed bootstrap) used to crash startup and Refresh, because only download errors were caught. Every failure now falls back to that currency's last stored curve, and is logged with its type.
  - A settlement date outside a bond's life (e.g. `DEBTLAB_SETTLEMENT_DATE` before the century bond's issue) stopped the app. That bond is now shown as "not priced" with the reason; the other is priced.
  - The quoting gate was worked out once per refresh, so a curve that went stale while the app ran kept saying "Quoting allowed". It is now re-applied on every read.
  - Valuation audit keys now include a digest of the results, so a valuation that changes on the same curve and date (edited terms, a pricing fix) is recorded. One extra valuation event per bond appears the first time this version runs on an existing database.
  - A NAV report that differs from a stored one with the same fund, as-of date and report time is now refused with an error, instead of silently ignored. A correction needs its own report time.
  - Month-end bonds: the first period of a bond issued 31 Aug and maturing 31 Aug was treated as a stub and underpaid its coupon (2.95 instead of 3.00). Fixed and checked against QuantLib. None of our instruments was affected; the century bond's real-curve figures are unchanged (clean 90.6201, yield 6.6188%).
  - The Refresh button stays usable after a failed refresh; `export_snapshot.py --full` no longer writes a file called `--full`; the curve discounting rule exists once instead of twice.
  - Not changed: a stalled source still blocks startup (risk R14, deferred to M9); pricing re-walks the coupon schedule many times (measured 0.35s per refresh for the century bond, so not worth optimising yet).
- Dashboard wording: the century bond's duration note now reads "(1 + yield)" rather than "(1 + yield/1)" for annual coupons. Spotted by Ethan.
- Line endings: `.gitattributes` keeps LF in the repo and working copy. `save_fixtures.py` now writes the ECB JSON with LF on Windows too (it was writing CRLF, an artefact of our script, not the ECB's data); the 30 Oct ECB fixture is back to LF, its content unchanged.

## 0.2.0-dev — 30 Oct 2026

Milestone 2 (complete).

- Dashboard: each bond panel now says which compounding its two duration figures use. On the century bond, modified duration (14.55) is per unit of the annually compounded yield; effective duration (15.54) bumps the continuously compounded gilt curve, which makes it close to Macaulay duration (15.51). The gap is the convention, not extra risk: restated per unit of annual yield it is 14.57. Spotted by Ethan. The API reports `curve_compounding` per bond. Header now reads "Milestone 2 complete".
- **Long-end extrapolation sensitivity (M2 step 5).** Curves can now extrapolate with a flat *forward* rate (from the last segment or a chosen window) as well as a flat zero rate, and can move the long-run forward up or down. Each bond that extends past its curve gets a table: seven rules, the rate each gives at maturity, the clean price, and the change against the current rule. Shown in the API (`long_end`) and on the dashboard. On the real 29 Oct gilt curve the century bond ranges from 89.56 (forward 6.33%) to 94.11 (forward 3.28%) around today's 90.62: a 5-point spread from a choice no market observes. Prices, IDs and the stored audit trail are unchanged; the table is information only. Flat-forward discount factors match QuantLib's log-linear extrapolation to 1e-14.

- **Par-to-zero bootstrap.** US Treasury par yields are now converted into zero rates (semi-annual grid, par yields interpolated linearly) before pricing. Matches QuantLib's bootstrapper to 1e-12, and every par bond reprices to par. On the real 29 Oct curve the 20-year zero rate is 18 bp above the par yield; the 3-year bond's price moved from 97.2944 to 97.2803.
- Curves now carry their own compounding convention (the bootstrapped Treasury curve is semi-annual). Curves that don't state one behave as before.
- `scripts/save_fixtures.py` saves the raw Treasury and ECB downloads into `tests/fixtures/real/`; `tests/test_real_fixtures.py` parses every saved file and skips cleanly when none exist.
- Fixed (found by Ethan in the audit log): a re-processed curve with the same source and date reused the old ID, so the bootstrapped curve and the changed valuation were silently not recorded. Curve IDs now include a fingerprint of the content. Stored par curves used as a fallback are bootstrapped on the way out.
- **Bank of England gilt curve.** Reads the nominal spot curve (0.5–40 years) from the BoE's daily "latest yield curve data" zip, laid out exactly as Ethan's probe showed on 30 Oct. Blank cells are skipped; an empty or sparse latest day falls back to the previous one. The century bond can now be priced from real UK rates.
- **Continuous compounding.** The BoE states its spot rates are continuously compounded; the ECB's curve is labelled the same way. Curves can now say so, and pricing uses DF = e^(−r·t). Checked against QuantLib to 1e-10.
- `save_fixtures.py` also saves the raw BoE zip. New dependency: `openpyxl`.
- `scripts/probe_treasury.py` kept as a diagnostic for download problems; `scripts/probe_boe.py` (Ethan's probe of the BoE file layout) added alongside it.
- **First real BoE run (30 Oct, Ethan's machine).** `save_fixtures.py` saved all three sources; the BoE zip parsed to the 29 Oct curve with 80 maturities (0.5–40Y; 10Y 5.28%, 20Y 5.74%, 40Y 5.33%). In online mode the century bond is priced from it: clean 90.6201, yield 6.62%, modified duration 14.55, DV01 0.1369, 8.3% of value beyond 40 years. All hand-checked.
- The three real fixtures from 30 Oct are committed, so the real-file tests run on any clone. `test_real_boe_file` now also checks the saved BoE curve is continuous with an unbroken half-year grid.

## 0.1.1 — 30 Oct 2026

- Fixed: on Windows the dashboard showed garbled characters (`Â·`, `âˆ’`) because files were read with the operating system's default encoding. Every file read and write now uses UTF-8 explicitly, and a new test fails if any doesn't. Found by Ethan running it on Windows.
- Fixed: US Treasury downloads timed out. Measured on Ethan's machine: plain requests took ~17s (over the 15s limit), browser-like requests 0.4s. Requests now send a browser User-Agent and allow 30s.
- Verified on real data (30 Oct 2026, Ethan's machine): the ECB adapter loaded the 29 Oct euro AAA curve (11 tenors); the Treasury CSV format matches the parser (latest row 10/29/2026, 10Y 4.98%, 30Y 5.32%).
- The app now answers browser favicon requests with an empty response instead of a 404.

## 0.1.0-m1 — 29 Oct 2026

Milestone 1: pricing slice.

- Improved project brief (`PROMPT.md`). Fixes: the XRP Ledger has no general smart contracts on mainnet, so the plan uses native features; the century bond is sterling, so it needs a gilt curve; right-sized stack (SQLite first, PostgreSQL at deployment); named data sources and their limits; concrete default limits for the €50 run; consolidated approval gates; milestone acceptance criteria.
- Decimal-based pricing engine: day counts (ACT/ACT ICMA, 30/360, ACT/365F), coupon schedules, curve interpolation with flat extrapolation, dirty/clean price, accrued interest, yield to maturity, Macaulay and modified duration, convexity, DV01, effective duration and convexity, rate and spread shocks with duration-convexity comparison.
- Two simulated instruments: a sterling century bond and a three-year US dollar corporate bond, each with documented assumptions.
- Market data: US Treasury and ECB adapters (not yet verified against live downloads), illustrative curves for offline use, staleness status, and a risk gate.
- SQLite storage with migrations, an append-only audit log with idempotency keys, and stored curve snapshots that survive outages.
- FastAPI app (`/api/health`, `/api/overview`, `/api/events`, `/api/refresh`) and a dashboard.
- 33 automated tests, including QuantLib reference checks.
