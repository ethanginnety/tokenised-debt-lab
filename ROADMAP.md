# Roadmap

A milestone is done only when its tests pass and its behaviour has been demonstrated.

| # | Milestone | Status | Done when |
|---|---|---|---|
| M1 | Pricing slice | **Done** (29 Oct 2026) | App runs; century and short bonds priced; tests pass against QuantLib; dashboard shows prices, risk, shocks, assumptions; audit log records valuations. |
| M2 | Real curves | **Done** (30 Oct 2026) | US, EUR and GBP adapters verified against real downloads; Treasury par-to-zero bootstrap; staleness gating; long-end extrapolation sensitivity shown. |
| M3 | Instruments and ledger | **Done** (30 Oct 2026) | Three instruments; simulated holder ledger; coupons, maturity and default with idempotent payments. |
| M4 | Market maker (simulation) | **In progress** (plan approved 30 Oct 2026) | Quotes with full records; limits, pause and kill switch; simulated fills with adverse selection; P&L split. |
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

## M3 plan

Approved by Ethan on 30 Oct 2026, with one change: lifecycle scenarios run in a **separate scenario database** (`data/scenario.sqlite3`), created fresh by a script, so the main database keeps a performing short bond for the M4 market maker. The dashboard shows which database it is reading.

1. ~~Instrument C, the private-credit fund share~~ (done 30 Oct: reported NAV with as-of date, report time, age and staleness; never quoted off).
2. ~~Holder ledger~~ (done 30 Oct: double-entry journal, four fictional test holders, balances as of any date, transfers, `scripts/new_scenario_db.py`, main/scenario role shown on the dashboard).
3. ~~Coupons~~ (done 30 Oct: record-date rule, per-holder entitlements, idempotent payment proven by a crash-and-restart test, Dana recorded unpaid, books closed at the record date, `scripts/lifecycle.py`).
4. ~~Maturity~~ (done 30 Oct: final coupon then principal, paid once per holder with units retired in the same transaction, status *matured*, unpaid amounts recorded; crash-and-restart tested).
5. ~~Default scenario~~ (done 30 Oct: manual trigger, missed coupon, 30-day grace, declaration, flat pricing at +1,000 bp then on the 40.90 recovery value, recovery after 12 months with units retired, per-holder losses, all audited; record dates now count from the scheduled date).
6. ~~Dashboard section and API~~ (done 30 Oct, read-only as proposed: `/api/lifecycle`, a `lifecycle` block in `/api/overview`, the *Lifecycle scenario* section; advancing and triggering stay in the CLI). M3 explanation in README.md.

## M3 evidence

- 151 automated tests pass (see [TESTING.md](TESTING.md)), including crash-and-restart tests for coupons, principal and the recovery (each rerun pays only the holders not yet paid; a third run changes nothing), and a test that the lifecycle API changes no row of any table.
- Live runs with `scripts/lifecycle.py` of the coupon, maturity and default scenarios on fresh scenario databases, figures in CHANGELOG: August 2026 coupon $202,500 of $225,000 paid (Dana on hold); short bond matured with Dana owed $1,135,000; default priced 86.68 flat in grace and 35.80 after declaration (illustrative curves), recovery 40.90 per 100, 60% loss per holder, Dana owed $431,500. Ledger integrity ok throughout.
- The lifecycle API ran against copies of Ethan's real main and scenario databases (pages 200).
- Ethan checked the dashboard, including the lifecycle section, in a browser and marked M3 done on 30 Oct 2026.

## M4 plan

Approved by Ethan on 30 Oct 2026 with his answers to five decisions and three changes (marked *Ethan*). Each step: tests first, run everything, update the docs, commit.

1. ~~**Ledger trades with a cash leg; fix R15.**~~ (done 30 Oct: `ledger.trade`, R15 fixed, migration 007, book in `config/marketmaker.json`; starting values, limits, tick, sizes, fill-rate k and the century add-on weight 0.5 approved by Ethan, see ASSUMPTIONS.) Refuse any unit movement in a bond dated after its final record date, at posting (only redemptions after it). `ledger.trade()`: delivery versus payment, all or nothing, idempotent by trade id. **Quotes are clean prices; the cash leg is clean + accrued, except a bond in grace period or default, which trades flat** (*Ethan*; both cases tested). New parties: *Market maker (simulated)* and *Simulated market* (the counterparty to every client order), funded with cash from a *funding* account that may go negative in cash only (decision 1, *Ethan*: yes). The four test holders don't trade. Starting values for the book, limits, tick, minimum order size and fill-rate k proposed with this step.
2. ~~**Quote model** (pure functions).~~ (done 30 Oct: `marketmaker/quotes.py`; spread parameters approved by Ethan, see ASSUMPTIONS.) Fair value, shifted against inventory; half-spread = sum of recorded components: base cost, duration (DV01 × assumed intraday yield move), credit, data age, inventory imbalance, illiquidity, fees, and **long-end model risk** on the century bond: weight × half the price gap between the "long-run forward −50 bp" and "+50 bp" cases, recomputed every quote, weight configurable (decision 2, *Ethan*). The full extrapolation range is shown on the dashboard as context only. Tick and minimum order size respected; every component and reason recorded.
3. ~~**Limits, pause, kill switch**~~ (done 30 Oct: `controls.py`, `execution.py`, migration 008; live check on a copy of the real main database.), persisted and versioned, every change audited, enforced by one execution-layer check before any order: inventory per instrument, portfolio exposure (market value and DV01), order size, daily loss, open orders, data age, minimum cash reserve. Exposure and daily loss **per currency, no FX** (decision 3, *Ethan*). Kill needs an explicit reset with a reason; restart tests.
4. **Quote nothing when invalid**: illustrative, stale or missing curve; failed valuation; ledger integrity or insufficient cash/units; a limit or state; the fund share; *halted*. *Restricted* (grace period): only the inventory-reducing side, at 2× spread and ½ size (decision 4, *Ethan*: yes). Every "no quote" recorded with its reason.
5. **Simulated market.** Seeded clock and random walk in the benchmark (labelled simulated). **Uninformed clients are price-sensitive: the probability of a fill is exp(−k × half-spread in bp), k configurable per instrument** (*Ethan*: otherwise a wider spread is free money); tested that a wider spread gets fewer uninformed fills. Informed clients see moves before the bot (adverse selection). Fees per fill; slippage when the bot unwinds. Fills posted as idempotent trades; crash-and-restart tested. Runs in the main database; a scenario run shows *restricted* and *halted*.
6. **P&L and CLI.** **Total = spread captured + inventory MTM − fees − slippage, slippage its own line, tested to the cent** (*Ethan*). Markouts measure adverse selection. `scripts/mm.py run|status|pause|resume|kill|reset|limits`.
7. **API, dashboard, explanation.** Read-only `/api/marketmaker` and a *Market maker* section; **controls stay in the CLI** (decision 5, *Ethan*). Labelled "Simulated: not evidence of real profitability". README worked example: the same seed with and without informed flow. Strategy, risk check and venue behind interfaces the XRPL adapters (M7, M8) will implement.

Done when: tests pass, live CLI runs with figures in CHANGELOG, Ethan checks the page in a browser. Quoting needs a fresh official curve, so the live demo runs in online mode on Ethan's machine; tests use the real fixture curves with a controlled clock.

## Where we are (resume point, 30 Oct 2026)

Written so a fresh session (after `/clear`) can pick up exactly here. Read PROMPT.md, this file, ASSUMPTIONS.md and CHANGELOG.md first.

**M1, M2 and M3 are done.** 200 tests pass (151 at the end of M3). Every step is committed.

**Now: M4, the simulated market maker. The plan is approved (see M4 plan above); steps 1–3 are committed. Now: step 4, quote nothing when invalid. Step 4 must take the quote model's maximum inventory from the database limit, not from config, and step 6 must replace the caller-supplied daily P&L.** The brief's §5 is the spec: quotes with full records (fair value, bid, ask, spread in currency and bp, data timestamps, inventory, limits in force, the reason for each adjustment); spread widening with duration, credit risk, data age, inventory, illiquidity and costs; tick size and minimum order size; limits enforced in the execution layer and persisted (inventory per instrument, portfolio exposure, order size, daily loss, open orders, data age, cash reserve, pause/resume, kill switch); quote nothing when data, valuation, balances or a risk check is invalid; simulated fills, fees, slippage and adverse selection; P&L split into spread captured, inventory mark-to-market and fees, on the dashboard. Strategy and risk sit behind interfaces shared with the paper and live adapters (M7, M8). Things M4 must respect from earlier milestones:
- `risk_increasing_allowed()`: illustrative, missing or stale curves block quoting (re-applied on every read, `with_current_gate`).
- The fund share is never quoted off its NAV (secondary price comes in M5).
- In a scenario database, `scenario_price` gives each bond's quoting state: *normal*, *restricted* (grace period; M4 defines what that allows) or *halted* (defaulted, matured).
- Trades have a cash leg (`ledger.trade`, M4 step 1), and no units move after a bond's final record date (R15 fixed).
- The main database is never advanced: the market maker runs there on a performing bond.

**How the lifecycle works (build on it):** `debtlab/lifecycle.py`. `require_scenario()` guards everything. Simulation date in `db_meta`, moved by `advance_simulation`. A `Payment` (kind `coupon`, `principal` or `recovery`; record date from its `scheduled_date`) is fixed by `schedule()` and paid by `pay()`, one holder per `atomic` block. `pay_due()` builds the day's work in date order: payments, then for a defaulted bond `miss_payment`, `declare_default` and the recovery (built when its step runs). `instrument_status`: performing → grace_period → defaulted, or matured. `defaults` holds the terms fixed at the trigger. `scenario_price(conn, bond, curve, date)` prices every state; `default_losses()` and `unpaid()` report. CLI: `scripts/lifecycle.py status|pay|advance DATE|default BOND [--db PATH]`. `service.lifecycle_view` reads it all for `GET /api/lifecycle`, read-only, in one read transaction.

**Working conventions:** small steps, tests first, run everything, update the docs (CHANGELOG, ASSUMPTIONS, TESTING, ARCHITECTURE, ROADMAP), commit after each step. Ethan's git identity is now set globally. `.gitattributes` keeps LF line endings: write files with the editor tools or `open(..., newline="")`, since plain `write_text` writes CRLF on Windows. There is no JavaScript engine in Claude's environment, so dashboard changes need Ethan to look at them in a browser (esprima can still syntax-check the page script: `pip install --target <scratch> esprima`).

**Not confirmed in a browser:** the illustrative-rates banner's per-currency failure list in online mode (it only appears when a source fails).

**Open items:** confirm the ECB compounding convention (below); risk R14, a stalled source blocking startup (M9). R15 fixed in M4 step 1.

Open M2 item: read the ECB technical notes to confirm its spot rates are continuously compounded (currently taken from the data portal's label).
