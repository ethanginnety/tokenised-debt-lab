# Roadmap

A milestone is done only when its tests pass and its behaviour has been demonstrated.

| # | Milestone | Status | Done when |
|---|---|---|---|
| M1 | Pricing slice | **Done** (29 Oct 2026) | App runs; century and short bonds priced; tests pass against QuantLib; dashboard shows prices, risk, shocks, assumptions; audit log records valuations. |
| M2 | Real curves | **Done** (30 Oct 2026) | US, EUR and GBP adapters verified against real downloads; Treasury par-to-zero bootstrap; staleness gating; long-end extrapolation sensitivity shown. |
| M3 | Instruments and ledger | **In progress** | Three instruments; simulated holder ledger; coupons, maturity and default with idempotent payments. |
| M4 | Market maker (simulation) | Planned | Quotes with full records; limits, pause and kill switch; simulated fills with adverse selection; P&L split. |
| M5 | Private credit depth | Planned | Withdrawal windows, caps, queue and gates; NAV vs secondary price; discount tests. |
| M6 | XRPL testnet | Planned | Bonds as labelled testnet tokens; coupon transfers on testnet; reconciliation. |
| M7 | RLUSD/XRP read-only and paper | Planned | Verified identifiers and depth; live book monitor; several days of paper results. |
| M8 | €50 live | Gated | Only after Ethan's explicit approval and all safety tests passing. |
| M9 | Persistent deployment | Planned | Docker Compose, PostgreSQL, worker, scheduler, heartbeats, alerts, tested restart recovery. |

## M1 evidence

- 33 automated tests pass (see [TESTING.md](TESTING.md)). Prices, accrued interest, Macaulay and modified duration, and convexity match QuantLib to better than 1e-9 on five bonds, including a short first coupon period and a 100-year bond.
- The server was started and `/api/health`, `/api/overview` and `/` all returned 200. Valuations were recorded in the audit log, and a refresh on identical data did not duplicate them.

## M2 plan (next)

1. ~~Run the Treasury and ECB adapters on real data~~ (done 30 Oct: both work; Treasury needed a browser-like request). Save the raw responses as fixtures with `scripts/save_fixtures.py`: all three saved on Ethan's machine 30 Oct.
2. ~~Bootstrap Treasury par yields to zero rates~~ (done 30 Oct; matches QuantLib to 1e-12).
3. ~~Build the Bank of England gilt adapter~~ (built 30 Oct from Ethan's probe of the real file; verified on the real 29 Oct file, 30 Oct).
4. ~~Confirm compounding conventions~~ (BoE confirmed continuous; ECB labelled continuous, to confirm in its technical notes).
5. ~~Add an extrapolation sensitivity table~~ (done 30 Oct: seven rules, API and dashboard).

## M2 evidence

- Treasury, ECB and Bank of England adapters fetched real data on Ethan's Windows machine on 30 Oct 2026; the raw files are committed in `tests/fixtures/real/` and parsed by tests.
- Par-to-zero bootstrap matches QuantLib to 1e-12; continuous discounting to 1e-10; flat-forward extrapolation to 1e-14.
- Online mode prices the century bond from the real 29 Oct gilt curve; figures hand-checked (see TESTING.md). Staleness gating tested since M1.
- Marked done by Ethan on 30 Oct 2026. The dashboard's long-end table has not been checked in a browser by Claude (no JavaScript engine available).

## M3 plan (current)

Approved by Ethan on 30 Oct 2026, with one change: lifecycle scenarios run in a **separate scenario database** (`data/scenario.sqlite3`), created fresh by a script, so the main database keeps a performing short bond for the M4 market maker. The dashboard shows which database it is reading.

1. ~~Instrument C, the private-credit fund share~~ (done 30 Oct: reported NAV with as-of date, report time, age and staleness; never quoted off).
2. ~~Holder ledger~~ (done 30 Oct: double-entry journal, four fictional test holders, balances as of any date, transfers, `scripts/new_scenario_db.py`, main/scenario role shown on the dashboard).
3. ~~Coupons~~ (done 30 Oct: record-date rule, per-holder entitlements, idempotent payment proven by a crash-and-restart test, Dana recorded unpaid, books closed at the record date, `scripts/lifecycle.py`).
4. ~~Maturity~~ (done 30 Oct: final coupon then principal, paid once per holder with units retired in the same transaction, status *matured*, unpaid amounts recorded; crash-and-restart tested).
5. Default scenario on the short USD bond, triggered manually: missed coupon, 30-day grace, default declared, quoting halted, spread repriced (1,000 bp, then recovery basis), 40% recovery of face + missed coupons after 12 months, per-holder losses, all audited.
6. Dashboard section (holders, payments, default state), API endpoints, docs, demo.

## Where we are (resume point, 30 Oct 2026)

Written so a fresh session (after `/clear`) can pick up exactly here. Read PROMPT.md, this file, ASSUMPTIONS.md and CHANGELOG.md first.

**Done in M3 so far:** steps 1-4, plus the ultrareview fixes (`c77f8e8`; finding 6 deferred as risk R14, finding 10 declined after measuring 0.35s) and the offline-banner wording fix. 132 tests pass. Every step is committed; the working tree is clean. Ethan has seen the database chip in a browser (main and scenario).

**How steps 3-4 work (build on it):** `debtlab/lifecycle.py`. `require_scenario()` guards everything. Simulation date in `db_meta` (`simulation_date`), set by `create_scenario_db`, moved by `advance_simulation`. A `Payment` (kind `coupon` or `principal`) is scheduled by `schedule()`, which fixes entitlements from `ledger.holdings(as_of=record_date)` (Dana stored as `unpaid` with a reason), and paid by `pay()`, one holder per `atomic` block (ledger txn `<payment id>:<holder>`, kind `coupon` or `redemption`, plus the paid mark); `_finish` sets *paid* / *partly paid*. `pay_due()` runs every due payment in date order. After principal, `_mark_matured` writes `instrument_status`. `lifecycle.unpaid()` lists amounts owed. `payment_events.kind` has no CHECK; its status CHECK already allows `missed`, and `instrument_status` allows `defaulted`. `ledger._books_open` refuses unit movements dated on or before any scheduled record date, and anything but a redemption once principal is scheduled. CLI: `scripts/lifecycle.py status|pay|advance DATE [--db PATH]`.

**Next: step 5, the default scenario on the short USD bond.** Agreed outline (from the plan above; propose the details to Ethan before building):
- Triggered manually (a CLI command such as `lifecycle.py default SHORT-USD-2029`), only in a scenario database, only on a performing bond; recorded with the simulation date.
- The next coupon is *missed*: scheduled as usual, nothing paid, entitlements unpaid with reason "issuer missed payment". `pay_due` must not pay coupons of a bond flagged as missing payments.
- 30-day grace period, then a default declaration: status *defaulted*, quoting halted, spread repriced (1,000 bp, then the recovery basis).
- 40% recovery of face + missed coupons, paid 12 months after the declaration, per holder (Dana's share recorded unpaid like her other amounts), units retired; per-holder loss = claim − recovery. All audited.
- Needs a design decision: how repricing at the simulation date is shown (today's curve held constant), and where a defaulted bond's price comes from (recovery basis: 40 × discount factor to the recovery date). Probably shown in step 6.

**Then:** step 5 default scenario, step 6 dashboard section and API (see the plan above). Assumptions already approved: 30-day grace, 1,000 bp distressed spread, 40% recovery of face + missed coupons, 12-month recovery delay, scenario reset by recreating the scenario database. Repricing at a future simulation date uses today's curve held constant (say so on the dashboard; step 6).

**Working conventions:** small steps, tests first, run everything, update the docs (CHANGELOG, ASSUMPTIONS, TESTING, ARCHITECTURE, ROADMAP), commit after each step. Ethan's git identity is now set globally. `.gitattributes` keeps LF line endings: write files with the editor tools or `open(..., newline="")`, since plain `write_text` writes CRLF on Windows. There is no JavaScript engine in Claude's environment, so dashboard changes need Ethan to look at them in a browser.

**Not yet seen in a browser:** the new illustrative-rates banner wording (offline mode, and the per-currency failure list in online mode).

**Open items:** confirm the ECB compounding convention (below); risk R14, a stalled source blocking startup (M9).

Open M2 item: read the ECB technical notes to confirm its spot rates are continuously compounded (currently taken from the data portal's label).
