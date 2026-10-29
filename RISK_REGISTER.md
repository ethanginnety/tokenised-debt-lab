# Risk register

Likelihood and impact are rated Low, Medium or High.

| # | Risk | Area | L | I | Mitigation | Status |
|---|---|---|---|---|---|---|
| R1 | Illustrative numbers mistaken for market data | Honesty | M | H | Banner, chips and "illustrative" status everywhere; risk gate blocks quoting on them; online mode never uses them. | Mitigated (M1) |
| R2 | Pricing convention error gives wrong prices | Financial | M | H | Independent QuantLib tests; hand checks (par bond, annuity formula). | Mitigated for M1 bonds |
| R3 | Source format changes or misparsed data | Data | M | H | Parsers raise rather than guess; fallback to last valid curve flagged by age; verify on first online run. | Open until M2 |
| R4 | Par yields used as zero rates | Financial | H | M | Documented; bootstrap in M2. | Open |
| R5 | Century bond value sensitive to extrapolation rule | Financial | H | M | Share of value beyond the curve shown on the dashboard; sensitivity table in M2. | Partly mitigated |
| R6 | Stale data drives trading | Financial | M | H | `risk_increasing_allowed()` gate; must be enforced in the execution layer (M4, M8). | Gate built; enforcement M4 |
| R7 | Duplicate coupon payment or order | Financial | M | H | Idempotency keys (proven for valuation events); extend to payments (M3) and orders (M4). | Pattern in place |
| R8 | Private key leaks | Security | L | H | Keys only from environment or signer, never logged; dedicated €50 wallet; review before M8. | Planned |
| R9 | Live bot loses more than intended | Financial | M | M | €50 ceiling enforced in the execution layer; order-size and daily-loss limits; kill switch; explicit approval gate. | Planned (M8) |
| R10 | RLUSD/XRP book too thin to trade sensibly | Market | M | M | Verify depth in read-only mode first; stop and report if inadequate. | Planned (M7) |
| R11 | Simulated tokens presented as real securities | Legal | L | H | Labels everywhere; testnet only; legal review required before anything real. | Mitigated |
| R12 | Process stops when no one is watching | Operational | H | M | Persistent deployment, heartbeats, alerts (M9). Until then the app only runs while you run it. | Open |
| R13 | Tax on live gains | Legal | M | L | Log every trade; Irish CGT applies to crypto disposals. | Noted |
