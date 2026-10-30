# Tokenised Debt Lab

A prototype marketplace for **simulated** tokenised debt: a bond-pricing engine, three simulated debt instruments, a market-making bot, coupon/maturity/default handling, and finally a tightly limited €50 experiment on the real RLUSD/XRP market on the XRP Ledger.

> **Educational prototype.** The instruments are simulations. Nothing here is a real security, an offer, or a public-blockchain asset.

**Status:** Milestone 1 (pricing slice) complete. See [ROADMAP.md](ROADMAP.md). The full brief is in [PROMPT.md](PROMPT.md).

## Quick start

You need Python 3.12 or newer.

```bash
cd tokenised-debt-lab
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
./scripts/test.sh                  # run all tests (Windows: python -m pytest)
python -m debtlab.api              # start the app
```

Open http://127.0.0.1:8000. By default the app runs **offline**, on illustrative curves that are clearly labelled as not market data.

To price from official daily data instead:

```bash
DEBTLAB_DATA_MODE=online python -m debtlab.api
```

Read "First online run" in [RUNBOOK.md](RUNBOOK.md) before relying on those numbers. Online mode currently has US Treasury and ECB adapters. The sterling (gilt) adapter is milestone M2, so until then the century bond shows "not priced" in online mode rather than guessing.

## Milestone 1 in plain English

**What we built.** A pricing engine that values a bond from its terms and a government yield curve, a web app that shows the results, and an audit log of everything it does.

**Why it matters.** Every later piece depends on knowing what an instrument is worth. The market maker quotes around this fair value, and the risk limits use its duration.

**How it works.**
1. Load each bond's terms from `config/instruments.json`.
2. Load a government curve per currency: illustrative offline, official online.
3. Build the bond's payment schedule backwards from maturity.
4. Discount every remaining payment at that date's government rate plus the bond's credit spread. The sum is the **dirty price**.
5. Subtract interest already built up since the last coupon (**accrued interest**) to get the **clean price**, which is how bonds are quoted.
6. Solve for the single **yield** that gives that price, then compute **duration** and **convexity** from it.
7. Reprice under rate and spread shocks.
8. Record a valuation event in the audit log.

### Worked example: the century bond (from an actual run, on illustrative rates)

| | Value |
|---|---|
| Coupon | 6.00% annual on 100 face |
| Accrued interest on 29 Oct 2026 | 3.5014 (213 days ÷ 365 × 6) |
| Fair clean price | 90.77 per 100 face |
| Yield to maturity | 6.61% |
| Modified duration | 14.6 |

What happens if government yields rise by 1 percentage point?
- **Duration alone** predicts a loss of about 14%.
- **Convexity** cushions that, because the price-yield relationship curves.
- **Full repricing** gives −12.3%.
- The same move costs the three-year bond only −2.1%.

That is the core of long-duration risk: distant payments are discounted over many years, so a small rate change compounds into a large price change.

**One surprise:** only about 4% of the century bond's value comes from payments more than 50 years away. At a 6–7% discount rate, distant money is worth little today. But that 4% depends entirely on an assumption, because no gilt curve reaches 100 years.

### Key terms

| Term | Plain English |
|---|---|
| Face value | The amount repaid at maturity. Prices are quoted per 100 of it. |
| Coupon | The fixed interest payment. |
| Yield curve | The government's borrowing rate for each length of time. |
| Credit spread | Extra yield investors demand for lending to a company rather than the government. |
| Dirty / clean price | What you pay including accrued interest / the quoted price without it. |
| Yield to maturity | The single interest rate that makes all future payments add up to today's price. |
| Duration | Roughly the % price change for a 1 percentage point change in yield. |
| Convexity | How much that relationship curves. It helps holders when rates move a lot. |
| DV01 | Price change for a 0.01% (1 basis point) yield move. |
| Basis point (bp) | 0.01%. 100 bp = 1 percentage point. |

## Project layout

```
debtlab/
  money.py              Decimal helpers (no floating-point money)
  pricing/dates.py      day counts, business days, coupon schedules
  pricing/curve.py      yield curves, interpolation, extrapolation
  pricing/bond.py       bond terms, coupon periods, accrued interest
  pricing/engine.py     prices, yields, duration, convexity, shocks
  marketdata/           curve sources (official + illustrative), staleness gate
  audit/db.py           SQLite migrations, audit events, stored curves
  service.py            loads curves, values instruments, records events
  ledger.py             double-entry holder ledger: balances as of a date, transfers, seeding
  scenario.py           creates fresh scenario databases for lifecycle demos
  lifecycle.py          simulation date, record dates, idempotent coupon payments (scenario only)
  instruments.py        loads config/instruments.json
  api/                  FastAPI app and the dashboard page
config/instruments.json simulated instruments and their assumptions
config/holders.json     fictional test holders and their starting positions
migrations/             database schema
tests/                  automated tests (QuantLib used as an independent reference)
scripts/                test runner, snapshot export, new_scenario_db.py, lifecycle.py
```

## Documentation

- [PROMPT.md](PROMPT.md): the project brief.
- [ROADMAP.md](ROADMAP.md): milestones and status.
- [ARCHITECTURE.md](ARCHITECTURE.md): how it fits together.
- [ASSUMPTIONS.md](ASSUMPTIONS.md): every modelling and technical assumption.
- [RISK_REGISTER.md](RISK_REGISTER.md): what could go wrong.
- [RUNBOOK.md](RUNBOOK.md): how to operate, recover and stop it.
- [TESTING.md](TESTING.md): how to test, and the latest results.
- [CHANGELOG.md](CHANGELOG.md): what changed.
