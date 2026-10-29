# Changelog

## 0.1.0-m1 — 29 Oct 2026

Milestone 1: pricing slice.

- Improved project brief (`PROMPT.md`). Fixes: the XRP Ledger has no general smart contracts on mainnet, so the plan uses native features; the century bond is sterling, so it needs a gilt curve; right-sized stack (SQLite first, PostgreSQL at deployment); named data sources and their limits; concrete default limits for the €50 run; consolidated approval gates; milestone acceptance criteria.
- Decimal-based pricing engine: day counts (ACT/ACT ICMA, 30/360, ACT/365F), coupon schedules, curve interpolation with flat extrapolation, dirty/clean price, accrued interest, yield to maturity, Macaulay and modified duration, convexity, DV01, effective duration and convexity, rate and spread shocks with duration-convexity comparison.
- Two simulated instruments: a sterling century bond and a three-year US dollar corporate bond, each with documented assumptions.
- Market data: US Treasury and ECB adapters (not yet verified against live downloads), illustrative curves for offline use, staleness status, and a risk gate.
- SQLite storage with migrations, an append-only audit log with idempotency keys, and stored curve snapshots that survive outages.
- FastAPI app (`/api/health`, `/api/overview`, `/api/events`, `/api/refresh`) and a dashboard.
- 33 automated tests, including QuantLib reference checks.
