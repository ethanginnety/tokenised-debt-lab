# Project brief: Tokenised Debt Lab

*Improved version of Ethan Ginnety's original brief, 29 October 2026. Give this whole file to any AI engineering agent working on the project.*

---

## 0. Who I am and what I want

I'm an MSc Quantitative Finance student building my **first crypto project**. I understand basic finance (bonds, yields, spreads) but I'm still learning advanced programming, quant libraries and blockchain infrastructure.

I want to finish with something I'm proud of: a working prototype of a **tokenised debt marketplace**, plus a small real-money experiment, that I fully understand and can explain in an interview or a write-up for my Substack, *The Inference Letter*.

Act as my senior quant engineer, backend developer, XRP Ledger integration engineer and project manager. Build it, test it, explain it, and leave me in control of it.

## 1. What we're building

Five components, built in this order:

1. **Bond-pricing engine.** Prices bonds from government yield curves, coupons, maturities and credit spreads.
2. **Three simulated debt instruments** with different risk and liquidity:
   - **A. Century bond.** A 100-year sterling corporate bond, modelled on (not representing) Alphabet's 2026 century tranche.
   - **B. Short-dated corporate bond.** About 3 years, so its rate sensitivity contrasts with A.
   - **C. Private-credit fund share.** Reported NAV, withdrawal windows, caps, queues and gates. It must behave differently from a bond.
3. **Lifecycle engine.** Coupons, maturity repayment and a manually triggered default scenario.
4. **Market-making bot.** Quotes bid and ask around fair value with inventory and risk limits, first in simulation.
5. **The €50 experiment.** The same strategy and risk architecture, pointed at the real RLUSD/XRP market on the XRP Ledger, through read-only mode, paper trading, and then tightly limited live trading after my approval.

**Simulated vs real.** Instruments A–C are simulations. They are never presented as real securities, legal claims or investments. If they are later issued as XRPL **testnet** tokens, they are labelled as testnet tokens with no value. Only the €50 experiment touches real assets.

## 2. Technical decisions already made

Use these unless you find a concrete reason not to. If you deviate, write down why.

- **Language:** Python 3.12+ for everything except the dashboard's browser code.
- **Backend:** FastAPI. **Dashboard:** server-served HTML with plain JavaScript and SVG charts. Move to TypeScript only if the dashboard outgrows that.
- **Storage:** SQLite with numbered SQL migrations while running locally. Switch to PostgreSQL when we deploy a persistent server. Keep the data-access layer thin so the switch is easy.
- **Deployment:** Docker Compose for the persistent phase.
- **XRPL:** `xrpl-py`. The XRP Ledger has **no general-purpose smart contracts on mainnet**. Use its native features (multi-purpose tokens, trust lines, the built-in order book and AMM, escrow) rather than writing an EVM/Solidity contract. Don't add a second blockchain without a strong reason.
- **Quant references:** QuantLib as an *independent test reference only*. The engine's own code must be readable and explainable without it.
- **Money and precision:** `Decimal` for prices, cash amounts and rates. Round only at display or settlement, with documented rounding rules.
- **Time:** all timestamps in UTC. Every market observation stores its source, observation date, fetch time, currency and units.
- **No microservices.** One application, one worker, one database.

## 3. Market data

Use official daily sources, and call them what they are: **daily published observations, not live ticks**.

| Curve | Source | Notes |
|---|---|---|
| USD | US Treasury daily par yield curve (CSV) | Par yields; must be **bootstrapped to zero rates** before discounting. |
| EUR | ECB euro area AAA government spot curve (data API) | Already spot rates; confirm the compounding convention. |
| GBP | Bank of England nominal gilt curves | Needed for the century bond. Published with a lag; confirm the format. |

Rules:
- Store the last valid curve. Mark it **stale** after a configurable age and show the age everywhere it's used.
- When data is stale or missing, **block risk-increasing actions**. Never fill gaps with invented numbers.
- No government curve reaches 100 years. Choose an explicit **long-end extrapolation rule** (start with flat extrapolation beyond the last tenor), show it on the dashboard, and test how sensitive the century bond's price is to it.
- An **illustrative curve** may be used only for offline development and tests. Wherever it's used, it must be labelled "ILLUSTRATIVE — not market data".

## 4. Quant requirements

Implement and test:
- Coupon schedules (generated backwards from maturity), business-day handling, and the day counts ACT/ACT (ICMA), 30/360 and ACT/365F.
- Accrued interest, dirty and clean price.
- Pricing from the curve: discount each cash flow at benchmark zero rate plus credit spread.
- Yield to maturity from a price; price from a yield.
- Macaulay and modified duration, convexity, DV01, and effective duration from curve bumps.
- Shocks: benchmark +25, +100 and −25 bp; spread +50 and +200 bp. Show the full repricing next to the duration-plus-convexity estimate, so I can see where the approximation breaks down.

Validate against QuantLib and hand-checkable cases (a par bond prices at par; closed-form annuity formula).

**Private-credit share (C):** reported NAV with its timestamp and age, withdrawal windows and caps, queue, lock-up, suspension rules, cash reserve, and a secondary-market price. Show separately: NAV, NAV age, secondary price, discount (in currency and %), and redeemable liquidity.

Discount to NAV = (NAV per share − secondary price) / NAV per share × 100%.

Never treat reported NAV as an executable price, and never call a discount an arbitrage.

## 5. Market-making engine

- Every quote records: fair value, bid, ask, spread (currency and bp), data timestamps, inventory, the limits in force, and the reason for any adjustment.
- The spread widens with duration, credit risk, data age, inventory imbalance, illiquidity and costs. Respect tick size and minimum order size.
- **Limits enforced in the execution layer and persisted** so a restart can't bypass them: maximum inventory per instrument, maximum portfolio exposure, maximum order size, maximum daily loss, maximum open orders, maximum data age, minimum cash reserve, pause/resume, and a kill switch.
- If data, valuation, balances or a risk check is invalid, the bot quotes nothing.
- The simulation models fills, fees, slippage and **adverse selection**. Report P&L split into spread captured, inventory mark-to-market and fees. Simulated P&L is never presented as evidence of real profitability.

## 6. Lifecycle

- **Coupons:** use a record-date rule, compute each holder's entitlement, and make payment idempotent (paying twice must be impossible). Partial failures must be recoverable.
- **Maturity:** settle principal once, change the instrument's status, and record anything unpaid.
- **Default (one bond, triggered manually):** missed coupon, default declaration, quoting halted or restricted, spread repricing, a recovery-rate assumption with a delay, and partial recovery, with accounting per holder.
- Explain the difference between a simulated default, a contractual event of default, insolvency, and a real issuer failing to pay.

## 7. The €50 RLUSD/XRP experiment

Run these stages in order, and don't skip any:

1. **Verify.** Use authoritative sources for RLUSD's issuer address and currency code, XRPL endpoints, current reserve requirements, fees, and order-book and AMM mechanics. Confirm that real RLUSD/XRP depth exists on the ledger's own order book. If it doesn't, stop and report. Don't switch pairs.
2. **Read-only.** Stream the order book, spread and depth, and log data outages.
3. **Paper trading** against live data for at least several days. Paper fills are labelled as paper.
4. **Live, after my explicit approval.** Use a dedicated wallet holding only the experiment funds. €50 is a ceiling, not a target. No leverage, no automatic transfers, no withdrawals. Keep reserves for the account and every open object. Default limits until I change them: maximum single order €10 equivalent, maximum daily loss €5, cancel all orders if data is older than 30 seconds or the connection drops. Reconcile every order with validated-ledger results and record transaction hashes.
5. **Report honestly:** starting and ending balances, orders, fills, cancels, failures, fees, P&L split, drawdown, uptime, and limit events. Report losses as plainly as gains.

The bond engine's fair values must never be used as prices for RLUSD/XRP. Use separate market-data and execution adapters behind the shared strategy and risk interfaces.

## 8. Approval gates (always ask me first)

- The first live mainnet order, and any increase in capital or limits.
- Any funding, transfer or withdrawal.
- Anything that issues tokens outside testnet, or presents an instrument to anyone else as investable.
- Never ask for, print or store my seed phrase or private key in chat, logs or the repo. Load signing keys from the environment or a secure signer.

Everything else (routine implementation choices) you decide, document in `ASSUMPTIONS.md`, and continue.

## 9. Milestones and acceptance criteria

A milestone is done only when its tests pass and its behaviour can be demonstrated. Files existing is not enough.

| # | Milestone | Done when |
|---|---|---|
| M1 | Pricing slice | App runs; century bond and short bond priced; tests pass against QuantLib; dashboard shows prices, risk, shocks and assumptions; audit log records valuations. |
| M2 | Real curves | USD, EUR and GBP adapters fetch real data on my machine; Treasury par bootstrap tested; staleness gating tested. |
| M3 | Instruments and ledger | All three instruments configured; simulated holders ledger; coupons, maturity and default tested, including idempotency. |
| M4 | Market maker (simulation) | Quotes with full records; limits and kill switch tested; simulated fills with adverse selection; P&L breakdown on the dashboard. |
| M5 | Private credit depth | Withdrawal queue and gates simulated; NAV vs secondary price chart; discount behaviour tested. |
| M6 | XRPL testnet | Bonds issued as clearly labelled testnet tokens; coupon payments as testnet transfers; reconciliation tested. |
| M7 | RLUSD/XRP read-only and paper | Verified identifiers; live book monitor; several days of paper results. |
| M8 | €50 live (gated) | Only after my approval and all safety tests passing. |
| M9 | Persistent deployment | Docker Compose; worker, scheduler, heartbeats, restart recovery, alerts, daily summary; runbook tested by an actual restart. |

## 10. How to explain things to me

For each milestone, cover: what we built, why it matters financially, how it works step by step, key terms in plain English, files changed, exact commands to run and test it, what success looks like, known limitations, and what's next.

Use worked examples with real numbers. Make sure I come away understanding:
- why bond prices fall when yields rise, and why a 100-year bond moves so much;
- why market makers widen spreads;
- why private credit can trade below its reported NAV;
- how fees and adverse selection turn an apparently profitable strategy into a losing one.

Show small code excerpts, not walls of code.

## 11. Honesty rules

- Don't claim anything works without having run it and recorded the evidence.
- Don't fabricate or silently substitute data, results or prices.
- Don't present simulated, paper or backtested results as real profitability.
- Disclose every limitation, including what this development environment can't do. For example, it may not be able to keep processes running after a session ends or reach external APIs. When that's the case, build the code and give me exact steps to run it myself.

## 12. Project files to maintain

`README.md`, `ROADMAP.md`, `ARCHITECTURE.md`, `ASSUMPTIONS.md`, `RISK_REGISTER.md`, `CHANGELOG.md`, `RUNBOOK.md`, `TESTING.md`, `.env.example`.

End every work session with: what was completed, actual test results, current running status, known bugs, decisions needing my approval, and the next task.
