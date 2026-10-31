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
4. ~~**Quote nothing when invalid**~~ (done 30 Oct: `quoting.py`, migration 009; quote model reads maximum inventory from the database limit.): illustrative, stale or missing curve; failed valuation; ledger integrity or insufficient cash/units; a limit or state; the fund share; *halted*. *Restricted* (grace period): only the inventory-reducing side, at 2× spread and ½ size (decision 4, *Ethan*: yes). Every "no quote" recorded with its reason.
5. ~~**Simulated market.**~~ (done 30 Oct: `simulation.py`, migration 010; the P&L split from step 6 was built here too, since Ethan asked for P&L in the comparison; approved with Ethan's three changes: calibration profiles, MTM split and many-seed table, unwind after a kill only on command.) Seeded clock and random walk in the benchmark (labelled simulated). **Uninformed clients are price-sensitive: the probability of a fill is exp(−k × half-spread in bp), k configurable per instrument** (*Ethan*: otherwise a wider spread is free money); tested that a wider spread gets fewer uninformed fills. Informed clients see moves before the bot (adverse selection). Fees per fill; slippage when the bot unwinds. Fills posted as idempotent trades; crash-and-restart tested. Runs in the main database; a scenario run shows *restricted* and *halted*.
6. ~~**CLI and the daily-loss feed**~~ (done 31 Oct: `daily.py`, migration 011, `scripts/mm.py`; the loss limit reads the day's P&L from the ledger and no caller can pass one in; live CLI runs on a copy of the real main database, figures in CHANGELOG.) (the P&L split and markouts were done in step 5). **Total = spread captured + inventory MTM − fees − slippage, slippage its own line, tested to the cent** (*Ethan*). Markouts measure adverse selection. `scripts/mm.py run|status|pause|resume|kill|reset|limits`.
7. **API, dashboard, explanation.** Read-only `/api/marketmaker` and a *Market maker* section; **controls stay in the CLI** (decision 5, *Ethan*). Labelled "Simulated: not evidence of real profitability". README worked example: the same seed with and without informed flow. Strategy, risk check and venue behind interfaces the XRPL adapters (M7, M8) will implement.

Done when: tests pass, live CLI runs with figures in CHANGELOG, Ethan checks the page in a browser. Quoting needs a fresh official curve, so the live demo runs in online mode on Ethan's machine; tests use the real fixture curves with a controlled clock.

## Ideas after M4

Not planned yet; to weigh once M4 is done.

- **Hedge the century bond's duration**, for example with a gilt future (long gilt or ultra-long), sized to its DV01. In the many-seed comparison below, the market move on the starting position dominates the P&L variance (about £12k a day s.d. in the normal profile, against about £4k of spread), so the bot's result is mostly an unhedged rates position. A hedge would show the market-making P&L on its own, and needs its own costs: basis between the century bond and the future, roll, margin.

## M4 step 5 evidence: many seeds

SIMULATED: not evidence of real profitability. `python scripts/mm_seeds.py --seeds 200 --out results/mm_seeds_2026-10-30.json`, 30 Oct 2026: 200 seeds per column, one simulated day each, on the official 29 Oct curves (BoE gilt spot, US Treasury par), 800 runs. Every run's numbers are in [`results/mm_seeds_2026-10-30.json`](results/mm_seeds_2026-10-30.json). Normal profile: 8.1 bp implied daily yield s.d.; stress: 16.3 bp. Informed clients arrive **on top** of the uninformed flow (4.5% a step per bond, a third of the uninformed rate), and the same seed gives the same market path and the same uninformed clients in both columns. In every run the P&L split equalled the total from cash and inventory exactly and the ledger passed its integrity check. Amounts in currency; fills are counts; "loss limit tripped" is the share of runs where the daily-loss limit paused the bot. No unwind in these runs, so slippage is 0.

| mean ± s.d. | normal, uninformed only | normal, + informed 4.5%/step | stress, uninformed only | stress, + informed 4.5%/step |
|---|---|---|---|---|
| GBP fees | 28 ± 9 | 28 ± 9 | 24 ± 12 | 27 ± 13 |
| GBP informed fills | 0.00 ± 0.00 | 0.20 ± 0.44 | 0.00 ± 0.00 | 0.88 ± 0.99 |
| GBP informed markout 30 steps | 0 ± 0 | -76 ± 214 | 0 ± 0 | -459 ± 943 |
| GBP informed markout 5 steps | 0 ± 0 | -78 ± 184 | 0 ± 0 | -379 ± 504 |
| GBP slippage | 0 ± 0 | 0 ± 0 | 0 ± 0 | 0 ± 0 |
| GBP spread | 4,278 ± 1,456 | 4,298 ± 1,475 | 3,492 ± 1,793 | 3,619 ± 1,874 |
| GBP start_mtm | -1,077 ± 12,121 | -1,077 ± 12,121 | -2,030 ± 24,785 | -2,030 ± 24,785 |
| GBP total | 3,358 ± 12,535 | 3,292 ± 12,528 | 1,962 ± 26,155 | 1,495 ± 26,051 |
| GBP traded_mtm | 185 ± 1,515 | 100 ± 1,589 | 524 ± 3,116 | -66 ± 3,064 |
| GBP uninformed fills | 10.96 ± 3.43 | 10.90 ± 3.42 | 9.37 ± 4.51 | 9.11 ± 4.45 |
| GBP uninformed markout 30 steps | 4,291 ± 1,646 | 4,288 ± 1,646 | 3,519 ± 2,242 | 3,512 ± 2,259 |
| GBP uninformed markout 5 steps | 4,251 ± 1,479 | 4,250 ± 1,488 | 3,466 ± 1,872 | 3,475 ± 1,893 |
| USD fees | 56 ± 14 | 57 ± 14 | 48 ± 19 | 51 ± 20 |
| USD informed fills | 0.00 ± 0.00 | 0.23 ± 0.49 | 0.00 ± 0.00 | 0.82 ± 0.95 |
| USD informed markout 30 steps | 0 ± 0 | -16 ± 40 | 0 ± 0 | -60 ± 105 |
| USD informed markout 5 steps | 0 ± 0 | -14 ± 33 | 0 ± 0 | -55 ± 69 |
| USD slippage | 0 ± 0 | 0 ± 0 | 0 ± 0 | 0 ± 0 |
| USD spread | 1,048 ± 272 | 1,049 ± 272 | 866 ± 361 | 859 ± 367 |
| USD start_mtm | -46 ± 1,711 | -46 ± 1,711 | -150 ± 3,367 | -150 ± 3,367 |
| USD total | 964 ± 1,764 | 942 ± 1,780 | 696 ± 3,442 | 610 ± 3,467 |
| USD traded_mtm | 19 ± 250 | -3 ± 272 | 28 ± 459 | -48 ± 506 |
| USD uninformed fills | 21.82 ± 4.97 | 21.74 ± 5.00 | 18.61 ± 7.19 | 18.32 ± 7.25 |
| USD uninformed markout 30 steps | 1,045 ± 299 | 1,044 ± 298 | 879 ± 418 | 873 ± 421 |
| USD uninformed markout 5 steps | 1,049 ± 275 | 1,048 ± 276 | 857 ± 367 | 855 ± 373 |
| loss limit tripped | 0.04 ± 0.21 | 0.04 ± 0.21 | 0.33 ± 0.47 | 0.34 ± 0.47 |

Paired by seed (with informed − without), mean ± standard error over 200 seeds:

| | normal GBP | normal USD | stress GBP | stress USD |
|---|---|---|---|---|
| total | −66 ± 31 | −22 ± 6 | −467 ± 126 | −86 ± 20 |
| spread | +20 ± 20 | +1 ± 3 | +127 ± 42 | −7 ± 7 |
| MTM on traded inventory | −85 ± 40 | −22 ± 7 | −590 ± 136 | −76 ± 20 |

Reading it: with informed flow on top, uninformed fills and markouts hardly change, so the gap between the columns is adverse selection. It shows up in *MTM on traded inventory*, not in spread: an informed fill on a stale quote looks like spread captured against the bot's own lagged fair value, then loses more than that as the mark catches up. The informed markouts measure the same loss directly (stress GBP: −459 at 30 steps vs −467 paired total). It is about seven times worse in stress than in normal for GBP, four times for USD. The market move on the starting position (identical in both columns) dominates the variance: the day's P&L is mostly the unhedged rates position in the £1m century bond (see Ideas after M4). In stress the £20k GBP loss limit pauses the bot in about a third of runs.

## Where we are (resume point, 30 Oct 2026)

Written so a fresh session (after `/clear`) can pick up exactly here. Read PROMPT.md, this file, ASSUMPTIONS.md and CHANGELOG.md first.

**M1, M2 and M3 are done.** 256 tests pass (151 at the end of M3). M4 steps 1–6 are done.

**Now: M4, the simulated market maker. The plan is approved (see M4 plan above); steps 1–4 are committed; step 5 (simulated market, P&L split, markouts, profiles) is committed; step 6 (daily loss from the ledger, `scripts/mm.py`) is done. Now: step 7, the read-only API and dashboard section.** The brief's §5 is the spec: quotes with full records (fair value, bid, ask, spread in currency and bp, data timestamps, inventory, limits in force, the reason for each adjustment); spread widening with duration, credit risk, data age, inventory, illiquidity and costs; tick size and minimum order size; limits enforced in the execution layer and persisted (inventory per instrument, portfolio exposure, order size, daily loss, open orders, data age, cash reserve, pause/resume, kill switch); quote nothing when data, valuation, balances or a risk check is invalid; simulated fills, fees, slippage and adverse selection; P&L split into spread captured, inventory mark-to-market and fees, on the dashboard. Strategy and risk sit behind interfaces shared with the paper and live adapters (M7, M8). Things M4 must respect from earlier milestones:
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
