# Tokenised Debt Lab

A prototype marketplace for **simulated** tokenised debt: a bond-pricing engine, three simulated debt instruments, a market-making bot, coupon/maturity/default handling, and finally a tightly limited €50 experiment on the real RLUSD/XRP market on the XRP Ledger.

> **Educational prototype.** The instruments are simulations. Nothing here is a real security, an offer, or a public-blockchain asset.

**Status:** Milestones 1 (pricing) and 2 (real curves) complete; Milestone 3 (instruments and ledger) complete. Milestone 4 (the simulated market maker) complete. Next: Milestone 5, private credit depth. See [ROADMAP.md](ROADMAP.md). The full brief is in [PROMPT.md](PROMPT.md).

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

Read "First online run" in [RUNBOOK.md](RUNBOOK.md) before relying on those numbers. Online mode uses the US Treasury, ECB and Bank of England (gilt) adapters. A source that fails falls back to its last stored curve, or leaves the bond "not priced"; it never guesses.

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

## Milestone 3 in plain English

**What we built.** The third instrument (a private-credit fund share), a ledger recording who holds what, and the lifecycle of a bond: coupons, repayment at maturity, and a default scenario you trigger by hand. The dashboard shows all of it, read-only.

**Why it matters.** Up to now a price was the only output. A real debt market also has to know *who* is owed each payment, pay it exactly once, and keep a correct record when things go wrong: a holder who can't be paid, a crash halfway through a payment run, or an issuer that stops paying. The market maker (M4) will trade against this ledger, and the XRPL tokens (M6) will mirror it.

### How it works, step by step

1. **Fund share (C).** Its only number is a NAV the manager reports: 10.2417 per share, valuing the portfolio at 30 Sep 2026, published on 24 Oct. The page shows its age (counted from 30 Sep, because that is how old the valuation is) and never quotes from it. A NAV is an appraisal, not a price anyone has agreed to pay.
2. **Ledger.** Four fictional holders get their bonds through one *issuance* transaction per instrument. Every later movement is a balanced transaction: what one account gains, another loses, so each asset sums to zero. The issuer's account is the only one allowed to go negative: −10,000,000 short-bond units means it has issued $10m face.
3. **Scenario database.** Lifecycle demos run in `data/scenario.sqlite3`, a throwaway copy with its own **simulation date**. The main database is never advanced, so the market maker keeps a performing bond.
4. **Coupons.** Each payment happens in two stages. *Scheduling* fixes who is owed what from holdings at the end of the **record date**. *Paying* then posts one transaction per holder. Each holder's cash and its "paid" mark are written together, or not at all.
5. **Maturity.** On the last date the final coupon is paid, then principal. Each holder's principal transaction pays the face amount *and* hands the units back to the issuer, so cash and units can't get out of step. The bond becomes *matured*.
6. **Default.** `lifecycle.py default SHORT-USD-2029` makes the next coupon go unpaid. A 30-day grace period runs, then default is declared. Twelve months later holders recover 40% of their claim, and the ledger works out each holder's loss.
7. **Pricing through the lifecycle.** On the simulation date the bond is priced according to its state, using today's curve held constant: normally while it performs, **flat** after the missed coupon, then at the value of the expected recovery.
8. **Dashboard.** `/api/lifecycle` and the *Lifecycle scenario* section show the simulation date, each bond's status and scenario price next to today's, the default timeline, every payment with each holder's entitlement, holders' positions and cash, and what is still owed. Nothing on the page changes the scenario: you move it with the CLI and reload.

### Worked example 1: a coupon, and why paying twice is impossible

The short bond pays 4.50% a year, semi-annually: **2.25 per 100 face** on 17 Aug 2026. Its record date is 15 days earlier, 2 Aug: whoever holds the bond at the end of that day is paid, even if they sell it afterwards.

| Holder | Face held on 2 Aug | Entitlement | Outcome |
|---|---|---|---|
| Alder Pension Fund | $4,000,000 | $90,000 | paid |
| Birch Family Office | $3,000,000 | $67,500 | paid |
| Cedar Insurance | $2,000,000 | $45,000 | paid |
| Dana Retail | $1,000,000 | $22,500 | **unpaid**: compliance hold |
| **Total** | $10,000,000 | $225,000 | $202,500 paid, *partly paid* |

Each entitlement is $\text{face} \times 2.25 / 100$, rounded to cents. Dana's $22,500 stays recorded as owed. It is never dropped.

**Idempotency** means running the same payment again changes nothing. A test cuts the run off after the second holder, reopens the database and runs it again: only Cedar is paid, because Alder's and Birch's entitlements are already marked paid, in the same transaction as their cash. A third run finds nothing due. This is what "partial failures must be recoverable" means in practice: the fix is always to run it again.

Scheduling also **closes the books**. Once the 17 Aug coupon is fixed, the ledger refuses any short-bond transfer dated on or before 2 Aug, since that would change after the fact who should have been paid.

### Worked example 2: maturity

Advancing to 20 Feb 2029 pays the remaining coupons, then principal. 17 Feb 2029 is a Saturday, so cash moves on Monday 19 Feb, but the record date still counts from the 17th: 2 Feb. Alder, Birch and Cedar get $9,000,000 and hand back their units. Dana's $1,000,000 can't be paid, so her units stay outstanding: the issuer still owes her. The bond is *matured* with **$1,135,000 owed to Dana** ($1,000,000 principal plus six coupons of $22,500).

### Worked example 3: the default, and what it costs each holder

Trigger the default on 30 Oct 2026, after the August coupon. The 17 Feb 2027 coupon is then missed. On the dashboard's illustrative curves:

| Date | What happens | Scenario price per 100 | Quoting |
|---|---|---|---|
| 16 Feb 2027 | Performing (one day before the coupon) | 101.82 dirty | normal |
| 17 Feb 2027 | Coupon missed, grace period starts | **86.68, flat** | restricted |
| 19 Mar 2027 | Default declared (day 30) | **35.80, flat** | halted |
| 20 Mar 2028 | Recovery paid, units retired | none left to price | halted |

Where the numbers come from:

- **In grace (86.68).** The rest of the bond (coupons and face, Aug 2027 to 2029) is discounted at government + 1,000 bp instead of + 110 bp: 84.43. Add the missed 2.25 at face, because it is owed now: 84.43 + 2.25 = **86.68**. The bond **trades flat**: no accrued interest is added, since nobody expects interest to arrive on schedule. A buyer pays 86.68 and takes over the claim to the missed coupon.
- **After declaration (35.80).** Holders now have a **claim** of face plus the missed coupon, 102.25 per 100. The assumed recovery is 40% of that: **40.90**. It arrives in 367 days, so it is discounted at the illustrative 1-year rate 3.70% + 10.00% = 13.70%, semi-annual: $40.90 \times (1 + 0.137/2)^{-2 \times 367/365} = 40.90 \times 0.8753 = 35.80$.
- **Losses.** Alder's claim is $4,000,000 × 102.25 / 100 = $4,090,000. It recovers 40% of that, $1,636,000, a **loss of $2,454,000 (60%)**. Every holder loses 60%. Dana's $409,000 recovery is on hold, so it shows as owed, not lost: she is owed **$431,500** in total (the August coupon plus her recovery). The missed February coupon is **written off**, because the recovery already paid 40% of a claim that included it; counting it separately would count it twice.

The price falling from about 100 to 36 isn't the loss. It is the market's estimate of the loss, made a year before the recovery is known. If the recovery turned out at 30%, holders who bought at 35.80 would lose too. All the default figures (30-day grace, 1,000 bp, 40%, 12 months) are assumptions, listed in [ASSUMPTIONS.md](ASSUMPTIONS.md). The section "What default means here" below explains how this simulation differs from a real one.

### Key terms

| Term | Plain English |
|---|---|
| NAV (net asset value) | The fund manager's own valuation of its holdings per share, as of a past date. Not a price anyone has agreed to trade at. |
| Double-entry ledger | Every movement is recorded twice, once as a gain and once as a loss, so nothing appears from nowhere. |
| Record date | The cut-off for who gets paid: holders at the end of that day, whoever holds the bond on the payment date. |
| Entitlement | One holder's share of one payment, fixed at the record date. |
| Idempotent | Doing it twice has the same effect as doing it once. |
| Trades flat | Quoted with no accrued interest, the convention for defaulted bonds. |
| Grace period | Time an issuer has to make a missed payment before it counts as a default. |
| Claim | What a holder is owed once a bond defaults: face plus unpaid interest. |
| Recovery rate | The share of the claim that holders eventually get back. |
| Distressed spread | The much higher spread used to discount a defaulted bond's uncertain payments. |
| Written off | Removed from what is owed because it has been settled another way (here, by the recovery). |

### Files

`debtlab/instruments.py` (fund share, NAV), `debtlab/ledger.py`, `debtlab/scenario.py`, `debtlab/lifecycle.py`, `debtlab/service.py` (`lifecycle_view`), `debtlab/api/app.py` (`/api/lifecycle`), `debtlab/api/static/index.html`, `config/holders.json`, `migrations/002`–`006`, `scripts/new_scenario_db.py`, `scripts/lifecycle.py`, and tests in `tests/test_fund.py`, `test_ledger.py`, `test_lifecycle.py` and `test_lifecycle_api.py`.

### Run it

```powershell
python -m pytest                                          # 151 tests
python scripts/new_scenario_db.py                         # fresh data/scenario.sqlite3 (stop the app first)
$env:DEBTLAB_DB_PATH = "data/scenario.sqlite3"; python -m debtlab.api
# in a second terminal, reloading the page after each command:
python scripts/lifecycle.py pay                           # August coupon; Dana unpaid
python scripts/lifecycle.py default SHORT-USD-2029
python scripts/lifecycle.py advance 2027-02-17            # missed, grace period, 86.68 flat
python scripts/lifecycle.py advance 2027-03-19            # declared, 35.80, quoting halted
python scripts/lifecycle.py advance 2028-03-20            # recovery, 60% losses, Dana owed $431,500
```

**What success looks like:** the *Lifecycle scenario* section moves through each row of the table above, the payments table opens to show each holder, the integrity line says every transaction balances, and running `pay` again changes nothing. On the main database the section shows only a line saying lifecycle is off.

### Known limitations

- The dashboard is read-only. Moving the date or triggering a default from the page would need its own design (who may rewrite history, and how it is audited).
- Future curves aren't modelled: prices hold today's curve constant. In offline mode the dashboard and the CLI can use different curves (both label theirs; see ASSUMPTIONS.md).
- No bank holidays, only weekends. A compliance hold can't be lifted, and late payment of an old coupon isn't modelled.
- The default can't be cured, recovery is a single cash payment, and the terms are fixed assumptions rather than an indenture.
- The fund share has no lifecycle yet: withdrawal windows, queues, gates and the secondary price come in M5.
- One transfer pattern can stall a scenario at maturity (risk R15, fixed when trading arrives in M4).
- The lifecycle section was checked in a browser by Ethan (30 Oct 2026), not by Claude, which has no JavaScript engine.

**Next:** M4, the simulated market maker, below.

## Milestone 4 in plain English

A market maker stands ready to trade both ways. It shows a **bid** (the price it will pay) and an **ask** (the price it will sell at), and earns the gap, the **spread**, when clients deal on both sides. Two things can go wrong: the market moves against the bonds it holds, and the client who deals may know something it doesn't (**adverse selection**). M4 builds one in simulation only.

> **SIMULATED: not evidence of real profitability.** The market, the clients and how they behave are all modelled. A positive number here says the model is consistent with itself, not that this strategy would make money on a real venue.

### How it works, step by step

1. **A quote.** Around its fair value from M1 (shifted slightly against its inventory), the bot sets a half-spread made of named parts: a base cost, duration risk, credit, data age, inventory, illiquidity, fees and, on the century bond, long-end model risk. Every quote, or the decision not to quote, is recorded with its reasons.
2. **Limits, checked before every order.** Inventory, exposure, order size, open orders, data age, cash reserve and **daily loss**, all stored in the database with who set them and why. The day's P&L for the loss limit is worked out from the ledger itself, so nothing the bot is told can hide a loss. A breach pauses the bot; the kill switch stops it until someone resets it with a reason.
3. **Quote nothing when unsure.** No quote on a stale, missing or illustrative curve, a bond that won't value, a failed ledger check, a halted bond, or the fund share. A bond in its grace period gets only an ask, at twice the spread and half the size.
4. **A simulated day.** 390 one-minute steps. Government yields follow a seeded random walk with occasional 8 bp jumps (about 8 bp a day in the *normal* profile, 16 bp in *stress*). The bot sees each move 3 minutes late. **Uninformed clients** arrive at random and are price-sensitive: the wider the spread, the less often they deal. **Informed clients** know the true value and deal only when the bot's quote is stale.
5. **The P&L split.** Spread captured + market move on the starting position + MTM on traded inventory − fees − slippage, per currency. It must equal the P&L worked out from cash and inventory alone, to the cent, and does in every run.

### Worked example: the same seeds with and without informed clients

Each column below runs the same seeds, so the market path and the uninformed clients are identical. The only difference is a 4.5% chance per bond per minute of an informed client arriving **on top**. Whatever changes is adverse selection. All figures are from `results/mm_seeds_2026-10-30.json` (`python scripts/mm_seeds.py --seeds 200`, on the official 29 Oct gilt and Treasury curves).

**One seed, to see the mechanism.** Stress profile, GBP, seed 153. It was picked by a rule, not by hand: the median result among the 68 stress seeds with exactly one informed GBP fill.

| GBP, seed 153, stress | without informed | with informed | difference |
|---|---:|---:|---:|
| Spread captured | 3,171.39 | 2,952.66 | −218.73 |
| Market move on the starting position | 25,207.47 | 25,207.47 | 0.00 |
| MTM on traded inventory | −1,147.26 | −1,226.01 | −78.75 |
| Fees | 24.78 | 29.60 | 4.82 more |
| **Total** | **27,206.82** | **26,904.52** | **−302.30** |
| Uninformed / informed fills | 8 / 0 | 8 / 1 | |
| Informed markout after 30 minutes | | −93.23 | |

The informed client dealt once, on a stale quote, and that fill alone was worth −£93 to the bot 30 minutes later. The rest of the £302 is a knock-on: the fill moved the bot's inventory, so its later quotes were skewed differently and the same eight uninformed clients dealt at worse prices for it. The total is dominated by the £25k market move on the £1m of century bond it started with, which is the same in both columns.

**One seed proves nothing, so here are 200 per column** (mean ± standard deviation over seeds; amounts in currency):

| | normal, uninformed only | normal, + informed | stress, uninformed only | stress, + informed |
|---|---:|---:|---:|---:|
| GBP spread captured | 4,278 ± 1,456 | 4,298 ± 1,475 | 3,492 ± 1,793 | 3,619 ± 1,874 |
| GBP market move, starting position | −1,077 ± 12,121 | −1,077 ± 12,121 | −2,030 ± 24,785 | −2,030 ± 24,785 |
| GBP MTM on traded inventory | 185 ± 1,515 | 100 ± 1,589 | 524 ± 3,116 | −66 ± 3,064 |
| GBP fees | 28 ± 9 | 28 ± 9 | 24 ± 12 | 27 ± 13 |
| **GBP total** | **3,358 ± 12,535** | **3,292 ± 12,528** | **1,962 ± 26,155** | **1,495 ± 26,051** |
| GBP informed fills | 0 | 0.20 ± 0.44 | 0 | 0.88 ± 0.99 |
| GBP informed markout, 30 min | 0 | −76 ± 214 | 0 | −459 ± 943 |
| USD spread captured | 1,048 ± 272 | 1,049 ± 272 | 866 ± 361 | 859 ± 367 |
| USD MTM on traded inventory | 19 ± 250 | −3 ± 272 | 28 ± 459 | −48 ± 506 |
| **USD total** | **964 ± 1,764** | **942 ± 1,780** | **696 ± 3,442** | **610 ± 3,467** |
| Daily-loss limit tripped (share of runs) | 4% | 4% | 33% | 34% |

The averages above are noisy because every run carries the same big rates position. Pairing each seed with itself removes that noise (with informed − without, mean ± standard error over 200 seeds):

| | normal GBP | normal USD | stress GBP | stress USD |
|---|---:|---:|---:|---:|
| Total | −66 ± 31 | −22 ± 6 | −467 ± 126 | −86 ± 20 |
| Spread captured | +20 ± 20 | +1 ± 3 | +127 ± 42 | −7 ± 7 |
| MTM on traded inventory | −85 ± 40 | −22 ± 7 | −590 ± 136 | −76 ± 20 |

**Reading it.**
- Adverse selection is real but small next to the rates position. In normal markets informed clients cost about £66 a day on the century bond. In stress the cost is about seven times larger (£467), because jumps leave quotes stale more often. Either is small next to the ±£12k–£26k daily swing from holding £1m of a 100-year bond unhedged.
- It shows up in *MTM on traded inventory*, not in spread. An informed fill looks like spread captured against the bot's own lagged fair value, then loses more than that as its mark catches up. The informed markouts measure the same loss directly: −£459 in stress GBP, against a paired total of −£467.
- The spread is the only reliably positive line, about £4k a day on the century bond and $1k on the short bond in normal markets. Whether that survives real clients, real fees and a hedge is exactly what this simulation can't tell you.

### Run it

```powershell
python -m pytest                                            # all tests
python scripts/mm.py status                                 # state, holdings, today's P&L from the ledger, limits, runs
python scripts/mm.py resume --reason "demo"                 # the bot starts paused
python scripts/mm.py run --seed 7                           # one simulated day, normal profile, on the latest official curves
python scripts/mm.py run --seed 7 --profile stress --informed 0.045 --id stress-7
python scripts/mm.py kill --reason "looks wrong"            # then: reset --reason, then resume --reason
python scripts/mm.py limits max_daily_loss GBP 15000 --reason "tighter"
python scripts/mm_seeds.py --seeds 200 --out results/x.json # the comparison above (several minutes)
$env:DEBTLAB_DATA_MODE = "online"; python -m debtlab.api     # the dashboard's Market maker section, read-only
```

`run` needs a fresh official curve, so start the app once in online mode first. To try it without touching your main database, copy `data/debtlab.sqlite3` and pass `--db` to the copy.

**What success looks like:** each run prints the P&L split with the ledger total beside it, and the two match. `status` shows today's P&L against the loss limit. The *Market maker* section shows the bot's state, today's P&L per currency with how much of the loss limit is used, the latest run's split, fills and markouts, the runs, the limits in force, recent quote decisions with reasons, and open orders.

### Files

`debtlab/marketmaker/` (`book.py`, `quotes.py`, `controls.py`, `execution.py`, `quoting.py`, `simulation.py`, `pnl.py`, `daily.py`, and `interfaces.py`: the strategy, risk check and venue seams the XRPL adapters will plug into), `debtlab/service.py` (`marketmaker_view`), `debtlab/api/app.py` (`/api/marketmaker`), `config/marketmaker.json`, `migrations/007`–`011`, `scripts/mm.py`, `scripts/mm_seeds.py`, `results/`, and tests in `tests/test_trading.py`, `test_quotes.py`, `test_controls.py`, `test_quoting.py`, `test_simulation.py`, `test_daily.py`, `test_mm_cli.py`, `test_marketmaker_api.py` and `test_interfaces.py`.

### Known limitations

- **Informed arrivals are random rather than news-driven.** In real markets informed flow clusters around news, exactly when quotes are most stale, so the adverse selection shown here is likely understated compared with real markets.
- The bot doesn't hedge, so the day's P&L is mostly the rates position on the century bond. A hedge with a gilt future would isolate the market-making result (see ROADMAP, Ideas after M4).
- Each run is one simulated day on today's curve. The simulated curve moves aren't stored, so after a run `status` and the dashboard value the bot's holdings at the real curve and show a different day's P&L from the run's own total (see ASSUMPTIONS, Daily loss).
- The bot marks its holdings at its own model prices; there is no market price to check them against.
- The dashboard is read-only. Every control stays in `scripts/mm.py`, where each change records who and why. The Market maker section was checked in a browser by Ethan (1 Nov 2026).

## What "default" means here

Four different things, easy to confuse:

- **Simulated default (this project).** A scenario switched on by hand in a throwaway database: the model's issuer skips a coupon, the clock runs on, and the ledger works out who is owed and loses what. No real company, contract or money is involved, and the numbers (30-day grace, 40% recovery, 12 months) are assumptions.
- **Contractual event of default.** A trigger defined in a real bond's indenture: non-payment after a grace period, breaking a covenant, cross-default on other debt, insolvency proceedings. It gives holders (usually through a trustee, often needing a vote of a share of holders) the right to accelerate: demand face plus accrued interest now. It is a legal right, not a payment.
- **Insolvency.** The issuer can't pay its debts as they fall due, or owes more than it owns. It usually leads to a court process (administration in the UK, Chapter 11 or 7 in the US) where creditors are paid in order of seniority, often years later. Recovery rates come from that process; 40% is a common rough figure for senior unsecured bonds, with a very wide range.
- **A real issuer failing to pay.** What actually happens is messier than any model: missed payments may be cured within the grace period, exchanged for new bonds in a distressed exchange, or settled in a restructuring. Credit-default-swap auctions, trustee actions and court rulings decide outcomes this simulation ignores.

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
  lifecycle.py          simulation date, record dates, idempotent coupons, maturity and default (scenario only)
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
