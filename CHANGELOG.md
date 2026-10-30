# Changelog

## 0.2.0-dev — 30 Oct 2026

Milestone 2, steps 1–2.

- **Par-to-zero bootstrap.** US Treasury par yields are now converted into zero rates (semi-annual grid, par yields interpolated linearly) before pricing. Matches QuantLib's bootstrapper to 1e-12, and every par bond reprices to par. On the real 29 Oct curve the 20-year zero rate is 18 bp above the par yield; the 3-year bond's price moved from 97.2944 to 97.2803.
- Curves now carry their own compounding convention (the bootstrapped Treasury curve is semi-annual). Curves that don't state one behave as before.
- `scripts/save_fixtures.py` saves the raw Treasury and ECB downloads into `tests/fixtures/real/`; `tests/test_real_fixtures.py` parses every saved file and skips cleanly when none exist.
- Fixed (found by Ethan in the audit log): a re-processed curve with the same source and date reused the old ID, so the bootstrapped curve and the changed valuation were silently not recorded. Curve IDs now include a fingerprint of the content. Stored par curves used as a fallback are bootstrapped on the way out.
- `scripts/probe_treasury.py` kept as a diagnostic for download problems.

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
