# Risk register

Likelihood and impact are rated Low, Medium or High.

| # | Risk | Area | L | I | Mitigation | Status |
|---|---|---|---|---|---|---|
| R1 | Illustrative numbers mistaken for market data | Honesty | M | H | Banner, chips and "illustrative" status everywhere; risk gate blocks quoting on them; online mode never uses them. | Mitigated (M1) |
| R2 | Pricing convention error gives wrong prices | Financial | M | H | Independent QuantLib tests; hand checks (par bond, annuity formula). | Mitigated for M1 bonds |
| R3 | Source format changes or misparsed data | Data | M | H | Parsers raise rather than guess; fallback to last valid curve flagged by age; verify on first online run. | Open until M2 |
| R4 | Par yields used as zero rates | Financial | H | M | Bootstrap to zero rates, checked against QuantLib. | Mitigated (0.2.0) |
| R5 | Century bond value sensitive to extrapolation rule | Financial | H | M | Share of value beyond the curve shown on the dashboard; sensitivity table in M2. | Partly mitigated |
| R6 | Stale data drives trading | Financial | M | H | `risk_increasing_allowed()` gate, re-applied on every read so a curve that ages after a refresh blocks quoting (fixed 30 Oct after review); must be enforced in the execution layer (M4, M8). | Gate built; enforcement M4 |
| R7 | Duplicate coupon payment or order | Financial | M | H | Idempotency keys (proven for valuation events); extend to payments (M3) and orders (M4). | Pattern in place |
| R8 | Private key leaks | Security | L | H | Keys only from environment or signer, never logged; dedicated €50 wallet; review before M8. | Planned |
| R9 | Live bot loses more than intended | Financial | M | M | €50 ceiling enforced in the execution layer; order-size and daily-loss limits; kill switch; explicit approval gate. | Planned (M8) |
| R10 | RLUSD/XRP book too thin to trade sensibly | Market | M | M | Verify depth in read-only mode first, against criteria fixed before looking (M7 decision C); stop and report if inadequate, no switching pairs. | Active (M7) |
| R11 | Simulated tokens presented as real securities | Legal | L | H | Labels everywhere; testnet only; legal review required before anything real. | Mitigated |
| R12 | Process stops when no one is watching | Operational | H | M | Persistent deployment, heartbeats, alerts (M9). Until then the app only runs while you run it. | Open |
| R14 | A source that stalls (rather than refusing) blocks startup and Refresh: fetches run one after another with retries, about 93s per request at worst and 11 ECB requests, so 15+ minutes | Operational | L | M | Found by the 30 Oct review. Deferred to M9, when fetching moves to the worker with an overall deadline and the app starts on stored curves. Until then: stop the app and use offline mode. | Open (M9) |
| R15 | A transfer dated after a bond's final record date, posted before its principal is scheduled, makes principal refuse to run, and with it every later payment in the scenario | Operational | L | L | Fixed in M4 step 1: the ledger refuses any unit movement (transfer or trade) dated after a bond's final record date, except redemptions, whatever has been scheduled. The M3 refusal at scheduling remains as a second check. | Fixed (M4 step 1) |
| R13 | Tax on live gains | Legal | M | L | Log every trade; Irish CGT applies to crypto disposals. | Noted |
| R16 | RLUSD's issuer can freeze or claw back RLUSD, or charge a transfer fee, so balances or costs change outside our control | Market / Legal | L | M | Issuer flags (freeze, clawback, transfer fee, tick size) read from the ledger in M7 step 1 and rechecked by the monitor; any transfer fee counted as a cost in paper P&L; reviewed again before M8. | Open (M7) |
| R17 | A public XRPL endpoint serves wrong, partial or unvalidated data | Data | L | H | Use only validated ledgers; read book and AMM at one ledger index per snapshot; detect sequence gaps; fail over to a second endpoint; log every outage. | Open (M7) |
| R18 | Paper fills flatter live results: queue position, market impact, other traders reacting, latency | Honesty | H | M | Two fill rules side by side (conservative and optimistic), the truth presented as between them; limitations stated on every paper report; paper results never presented as real profitability. Built (2 Nov): orders count only for later ledgers' trades, a cancel only from the next ledger, fills capped by funds; M8 runs paper alongside live on the same quotes to measure the gap. | Open (M7) |
| R19 | RLUSD trades away from $1, so euro limits converted at 1 RLUSD = $1 are wrong | Financial | L | M | The assumption is labelled wherever euro figures appear; EUR/USD from the ECB daily rate, stale after 96 hours; revisit with an observed RLUSD/USD price before M8. | Open (M7) |
| R20 | An offer takes liquidity despite `tfPassive`, or outlives the 30 s limit after a lost connection (M8) | Financial | M | M | No post-only flag exists, so: a price margin from the book and the AMM; pause on the first fill that takes liquidity. Every offer expires by itself on a measured worst case of 26.93 s; quoting stops if close-time resolution changes. Re-measured before any mainnet plan. | Open |
