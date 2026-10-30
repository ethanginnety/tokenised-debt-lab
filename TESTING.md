# Testing

## Run the tests

```bash
./scripts/test.sh          # macOS/Linux; writes test-results.xml
python -m pytest -v        # any platform
```

QuantLib is a test-only dependency, used as an independent check on the pricing maths. If it isn't installed, those reference tests are skipped, and the hand-check tests still run.

## What is tested

| Area | Tests |
|---|---|
| Dates | 30/360 edge cases, month-end clamping, weekend roll, century schedule (100 periods), short first stub. |
| Hand checks | Par bond prices at par; closed-form annuity formula; price↔yield round trip; price falls when yield rises; century duration below the perpetuity limit 1/y. |
| QuantLib reference | Clean price, accrued, Macaulay and modified duration, convexity on 5 bonds (ACT/ACT ICMA and 30/360, annual and semi-annual, a 100-year bond, a short stub). Tolerance 1e-9 (convexity 1e-6). |
| Curves and shocks | Tenor parsing, interpolation, flat extrapolation, parallel shift; shock signs; approximation close for small moves; century bond far more rate-sensitive than the 3-year; share of value beyond the curve; wrong-currency curve and out-of-range settlement rejected. |
| Market data | Treasury CSV uses the latest row and skips blank cells; garbage raises instead of inventing; ECB parse and mixed-date rejection; retries then a clean failure. |
| Staleness and risk gate | Fresh vs stale by age; illustrative, missing and stale data all block risk-taking. |
| Storage and recovery | Migrations apply once; idempotent events; events survive a reopened database; last valid official curve used when the source fails, flagged stale; no curve invented when none stored. |
| Bootstrap | Flat par curve gives the same flat zero curve; two-step hand example; every par bond on the real 29 Oct curve reprices to par (1e-24); real curve shape; matches QuantLib `PiecewiseLogLinearDiscount` to 1e-12; Treasury snapshots come out bootstrapped. |
| Real downloads | Every saved Treasury, ECB and BoE file parses and has sane levels; Treasury bootstraps cleanly; BoE is continuous with an unbroken half-year grid. Fixtures from 30 Oct are committed. |
| Long-end extrapolation | Flat-forward hand example (continuous) and constant forward on a semi-annual curve; flat zero equals flat forward at the last zero rate; long-run forward shift joins smoothly with published points untouched; parallel shifts keep the rule; bad settings raise; flat-forward discount factors match QuantLib `DiscountCurve` (log-linear) extrapolation to 1e-14 on all 60 payments past 40Y; sensitivity table: base row is today's price, price falls as the long-run forward rises, no table when the curve covers the bond, works on illustrative curves. |
| Gilts and compounding | Workbook built to the real BoE layout: latest day used, blank cells skipped, sparse day falls back, bad zips and empty months raise; continuous discount factors exact; continuous curve pricing matches QuantLib to 1e-10; century bond prices off a gilt curve with sensible duration and extrapolated share. |
| Fund share (C) | Config loads all three instruments; bad NAV reports rejected (non-positive, no timezone, reported before its as-of date); a report stored twice is kept once and the latest as-of wins; config NAV seeded and audited once across restarts; NAV age and days since reported; stale after 45 days; missing NAV; quoting never allowed; API lists the fund apart from the bonds. |
| Failure isolation | Any exception from a source falls back to the stored curve without losing other currencies; a bond outside its life is skipped; the gate follows the clock. |
| Ledger | Balanced transaction posts once and is audited; same id with different contents refused; unbalanced, zero, single-line, too many decimals, unknown party and holder-below-zero all rejected with nothing written; nested atomic rollback; exact decimal sums; seed matches config, conserves units and is idempotent; balances as of a date; bond positions must start on the issue date; transfers conserve units and keep history; bad and back-dated transfers rejected. |
| Databases | Role set once; scenario database created seeded, recreated from scratch with the old one archived; main and unmarked databases refused and left unmodified; app marks a new database main and reports its role; `;` in a migration comment. |
| Portability | Every file read/write in the code and tests states UTF-8 (a Windows-only bug otherwise). |
| API | Offline end to end (health, overview, events, refresh without duplicate valuations, dashboard page); online mode with all sources down leaves bonds unpriced; snapshot export escapes script tags. |

## Latest results

**30 Oct 2026 (M3 step 2, ledger): 113 passed, 0 skipped**, 1 warning (Starlette). Live: `new_scenario_db.py` created a scenario database (4 holders, integrity ok), a second run archived it and started fresh, and pointing it at the main database was refused. The app ran against a copy of the real main database (marked main, 3 issuance transactions, integrity ok, earlier events kept) and against the scenario database (header role "scenario"); both pages 200.

**30 Oct 2026 (review fixes): 86 passed, 0 skipped**, 1 warning (Starlette). New tests: any source failure type falls back per currency; bonds before issue / after maturity skipped while the other is priced; quoting gate re-applied as a curve ages (fake clock); changed terms on the same curve recorded; conflicting NAV report refused; month-end first period regular (hand test, both day counts) and matching QuantLib (ACT/ACT); snapshot export arguments. Century bond on the real 29 Oct gilt curve re-checked: clean 90.6201, yield 6.6188%, modified duration 14.5471, DV01 0.136943 (unchanged).

**30 Oct 2026 (M3 step 1, fund share): 71 passed, 0 skipped**, 1 warning (Starlette, not project code). Live server on a fresh database: `/api/overview` returned the fund (NAV 10.2417, age 30 days, quoting blocked) and the two bonds as before; one `nav_loaded` event; dashboard page 200. Migration checked on a copy of the real main database. **Not yet checked:** the fund card in a browser (no JavaScript engine here).

**30 Oct 2026 (long-end sensitivity): 62 passed, 0 skipped**, 1 warning (Starlette, not project code). Live API in online mode returned the 7-row table for the century bond off the real 29 Oct gilt curve, and none for the short bond; base price and curve ID unchanged. Hand check: +100 bp row, rate at maturity (5.327% × 40 + 6.327% × 59.48)/99.48 = 5.925% ✓. **Not yet checked:** the dashboard table in a real browser (no JavaScript engine in this environment).

**30 Oct 2026 (first real BoE run, Ethan's Windows machine, Python 3.14.6): 53 passed, 0 skipped**, 1 warning (Starlette test client deprecation, not project code). `save_fixtures.py` saved Treasury (14 maturities), ECB (11) and BoE (80).

Manual check, online mode: century bond priced from `GBP:Bank of England nominal gilt spot curve:2026-10-29` (fresh, 38h old). Independent plain-Python check of the app's figures: accrued 6 × 214/365 = 3.517808 ✓; re-discounting 100 cash flows at the 6.6188% yield gives dirty 94.1379 / clean 90.6201 ✓ (perpetuity 6/y = 90.65); Macaulay 15.5099 ✓ (below the perpetuity limit 16.11); modified 14.5471 ✓; DV01 0.136943 ✓.

**30 Oct 2026 (gilts): 49 passed, 3 skipped** (the 3 wait for real downloads).

**30 Oct 2026 (0.2.0-dev): 41 passed, 2 skipped.** The skipped tests check real downloads and run once `scripts/save_fixtures.py` has saved some.

**30 Oct 2026: 34 passed** after adding the UTF-8 portability test. Ethan's first run on Windows (Python via venv): 33 passed, 1 warning (from Starlette's test client, not project code); app started and dashboard loaded, which exposed the encoding bug fixed in 0.1.1.

**29 Oct 2026: 33 passed, 0 failed, 0 skipped** (Python 3.13, QuantLib 1.44).

Manual checks the same day:
- Started the server; `/api/health`, `/api/overview` and `/` returned HTTP 200.
- Hand-checked accrued interest: century bond 213/365 × 6 = 3.5014 ✓; short bond 30/360, 72 days × 4.5/360 = 0.9000 ✓.
- Rendered the exported dashboard once in a headless DOM: 2 bond panels, 5 shock rows, both charts drawn, audit log filled, no script errors.

## Not yet tested

- Visual rendering in real browsers (the headless DOM check doesn't measure layout).
- Coupons, default, market making and XRPL: later milestones.
