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
| Portability | Every file read/write in the code and tests states UTF-8 (a Windows-only bug otherwise). |
| API | Offline end to end (health, overview, events, refresh without duplicate valuations, dashboard page); online mode with all sources down leaves bonds unpriced; snapshot export escapes script tags. |

## Latest results

**30 Oct 2026: 34 passed** after adding the UTF-8 portability test. Ethan's first run on Windows (Python via venv): 33 passed, 1 warning (from Starlette's test client, not project code); app started and dashboard loaded, which exposed the encoding bug fixed in 0.1.1.

**29 Oct 2026: 33 passed, 0 failed, 0 skipped** (Python 3.13, QuantLib 1.44).

Manual checks the same day:
- Started the server; `/api/health`, `/api/overview` and `/` returned HTTP 200.
- Hand-checked accrued interest: century bond 213/365 × 6 = 3.5014 ✓; short bond 30/360, 72 days × 4.5/360 = 0.9000 ✓.
- Rendered the exported dashboard once in a headless DOM: 2 bond panels, 5 shock rows, both charts drawn, audit log filled, no script errors.

## Not yet tested

- Real downloads from the Treasury and ECB (the build environment had no access). See RUNBOOK, "First online run".
- Visual rendering in real browsers (the headless DOM check doesn't measure layout).
- Coupons, default, market making and XRPL: later milestones.
