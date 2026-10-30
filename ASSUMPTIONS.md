# Assumptions

Every modelling or technical choice made without explicit instruction. Each one can be changed. If you change one, record it here.

## Instruments (all simulated)

| Item | Assumption | Why |
|---|---|---|
| Century bond coupon | 6.00%, paid annually | The real Alphabet sterling tranche's coupon and frequency weren't in the sources checked. Sterling corporate bonds commonly pay annually. |
| Century bond issue date | 30 Mar 2026, maturing 30 Mar 2126 | Taken from reporting dated 31 Mar 2026; the exact date is unconfirmed. |
| Century bond spread | 95 bp over gilts | Plausible for a very highly rated issuer at a very long tenor. **Not observed.** |
| Century bond day count | ACT/ACT (ICMA) | Standard for sterling bonds. |
| Short bond | Single-A US corporate, 4.50% semi-annual, 30/360, 17 Feb 2026 to 17 Feb 2029, 110 bp spread | A generic contrast case. All values assumed. |
| Private-credit fund share | `PCF-USD-SIM`, US dollar. NAV 10.2417 per share as of 30 Sep 2026, reported 24 Oct 2026 16:00 UTC. All invented. | Gives instrument C a realistic shape: a monthly NAV published a few weeks late. |
| NAV staleness | Stale once the NAV's as-of date is more than 45 days old (monthly cycle plus a reporting lag of about two weeks). Age is counted from the as-of date, not the report date, because that is how old the valuation is. | Configurable per fund (`nav_stale_after_days`). |
| NAV and quoting | A reported NAV never allows quoting or trading. | The brief: NAV is not an executable price. A secondary-market price arrives in M5. |
| Latest NAV | The report with the latest as-of date; a later restatement of the same date replaces it. | |

## Ledger

| Item | Assumption | Why |
|---|---|---|
| Test holders | Alder Pension Fund, Birch Family Office, Cedar Insurance, Dana Retail, all fictional and labelled "(test)". Short bond $10m face (4/3/2/1m), century bond £5m (2/1.5/1.5m; Dana holds none), fund 100,000 shares (40/25/25/10k). | A few holders of different sizes are enough to check per-holder sums. |
| Payments blocked | Dana has a compliance hold: amounts owed to it will be recorded as unpaid, not paid (M3 step 3). | Gives "unpaid amounts" a concrete case outside default. |
| Starting positions | Created by an issuance transaction dated the bond's issue date (the fund: 2 Jan 2026). No cash changes hands at issue; holders start with no cash. | The lifecycle only needs units; cash arrives through payments. |
| Units | Bonds in face amount of their currency; fund in shares. Cash to 2 decimal places, units to 4. | |
| Issuer accounts | Only issuers may go negative; an issuer's balance is minus what it has issued (units) or paid out (cash). Holders can never go below zero. | Standard double entry; no short selling in a holder ledger. |
| No back-dating | A holder's movement can't be dated before an earlier movement in the same account. | Keeps every past balance, and so every record date, fixed once later activity exists. |
| Transfers | Between holders only, free of payment. Trading with a cash leg comes with the market maker (M4). | |
| Main database | Seeded with the same holders on start, but its simulation date is never advanced. | The market maker (M4) needs a performing short bond. |

## Lifecycle (scenario database only)

| Item | Assumption | Why |
|---|---|---|
| Simulation date | Starts on the day the scenario database is created, stored in `db_meta`, only moves forward, advanced by hand. Same ledger plus same date gives the same result. | Deterministic, repeatable demos. |
| Record date | Counted back from the **scheduled** payment date, before any weekend roll. Short USD bond: 15 calendar days before. Century GBP bond: 1 business day before (weekends only; no bank holidays). Per instrument in `config/instruments.json`. Holders at the end of the record date are paid. Example: 17 Feb 2029 is a Saturday, so the payment moves to Monday 19 Feb, but the record date stays 2 Feb. | US indentures count from the scheduled date (Ethan, 30 Oct; step 4 had counted from the rolled date, giving 4 Feb). Same rule for both bonds for consistency; for the century bond the two methods only differ when a payment date falls on a weekend. |
| Books close at the record date | Once a payment is scheduled, the ledger refuses movements in that bond dated on or before its record date. | Otherwise a back-dated transfer could change who "should" have been paid after the fact. |
| Entitlement | Units × coupon per 100 ÷ 100, rounded to cents per holder (banker's rounding). The exact total minus the rounded total is stored as the rounding residue. | Cash settles in cents; the residue makes rounding visible. |
| Payment date in the ledger | A coupon's ledger transaction is dated its scheduled payment date (after the weekend roll), even if the run happens later. | The coupon belongs to that date; late payment of an old coupon isn't modelled yet. |
| Due payments | Every coupon whose payment date is on or before the simulation date, oldest first, then principal on the maturity payment date (after the final coupon). | |
| Principal | Paid like a coupon: same record date, one atomic block per holder. Each holder's transaction pays face in cash *and* hands the units back to the issuer, so cash and units move together. | A redemption is an exchange; recording one leg without the other would leave the ledger wrong after a crash. |
| Maturity status | *Matured* once every principal entitlement is paid or recorded unpaid, dated the principal payment date, set once. Anything unpaid stays recorded as owed. Dana's units stay outstanding (the issuer still owes her $1m) until she is paid. | The brief: settle principal once, change status, record anything unpaid. |
| After maturity | Once principal is scheduled, the ledger allows no movement in the bond except redemptions. If units moved after the final record date but before principal was scheduled, principal is refused (the holders of record no longer hold what they would hand back). | A matured bond stops trading. The refusal stops the scenario at that point (see risk R15); recreate the scenario database to continue. |
| Blocked holder | Dana's entitlement is recorded as *unpaid (payments blocked)* when the coupon is scheduled; nothing is posted. The coupon's status is then *partly paid*. A hold can't be lifted yet. | Gives "unpaid amounts" a concrete case outside default. |
| Payment status | *scheduled* → *in progress* → *paid* (all holders) or *partly paid* (some still unpaid); *missed* when the issuer pays nobody (default scenario). | |
| Instrument status | `instrument_status` (migration 005, widened in 006): no row means *performing*; then *grace_period*, *defaulted* or *matured*. Moved from migration 004 to 005, since step 4 is the first to write it. | No unused table. |

## Default scenario (scenario database only)

Terms approved by Ethan on 30 Oct 2026, with his two corrections (recovery on the whole claim; trading flat). They are stored with each default when it is triggered.

| Item | Assumption | Why |
|---|---|---|
| Trigger | By hand (`lifecycle.py default SHORT-USD-2029`), on a performing bond with a coupon still to come. The **next coupon after the simulation date** is missed. Payments due before it are paid normally. | The brief: one bond, triggered manually. |
| Missed coupon | Scheduled as usual (record date, entitlements), then every entitlement is recorded *unpaid, issuer missed payment*; the payment is *missed*; nothing is posted to the ledger. The bond enters a **grace period**. | |
| Grace period | 30 days. The issuer can't cure it in this simulation, so default is declared on day 30 (17 Feb 2027 → 19 Mar 2027). | Typical bond indenture grace for interest. |
| After the missed coupon | No later coupon or principal is scheduled or paid. The claim is settled by the recovery alone. | Acceleration: after default, holders claim face plus unpaid interest rather than the original schedule. |
| Trades flat | From the missed coupon on, the bond **trades flat**: no accrued interest, so clean = dirty. The claim to the missed coupon goes with the bond to whoever holds it, and the price includes it. | Market convention for defaulted bonds (Ethan's correction, 30 Oct). |
| Price in grace | Remaining scheduled payments discounted at government zero rate + **1,000 bp** distressed spread, plus the missed coupon at face (owed now). Quoting *restricted* (M4 defines what that allows). | |
| Price after declaration | The recovery value of the claim: **40% × (face + missed coupons)** = 40% × 102.25 = **40.90 per 100**, discounted from the recovery payment date to the simulation date at government zero rate + 1,000 bp. Quoting *halted*. Not 40: the claim includes the missed coupon (Ethan's correction, 30 Oct). | The 40% is an assumption about outcome; the distressed spread prices the time and uncertainty until it arrives. |
| Recovery | 12 months after declaration (19 Mar 2028, a Sunday, paid Monday 20 Mar), 40.90 per 100 to the holders at its record date (15 days before 19 Mar: 4 Mar 2028). Each holder's transaction pays cash and returns the units together, like principal. | Recovery takes time; 12 months is short for a real workout. |
| Missed coupon after the recovery | Written off: the recovery is a share of the whole claim, missed coupon included, so the coupon isn't owed on top. Before the recovery is scheduled it shows as owed. | Otherwise it would be counted twice. |
| Per-holder loss | Claim (units × 102.25 / 100) minus the recovery entitlement. With no trading after the default, every holder loses 60% (Alder: claim $4,090,000, recovered $1,636,000, loss $2,454,000). Dana's recovery is owed but unpaid (hold), and shown as owed, not as a loss. | |
| Repricing at a future date | Today's curve, held constant: times are measured from the simulation date. The CLI uses the scenario database's latest stored official curve, else the illustrative one, labelled. | No forecast of future curves. |

## Pricing

| Item | Assumption |
|---|---|
| Curve rates | Each curve states its compounding. US Treasury: par yields bootstrapped to **semi-annual zero rates**. Bank of England: **continuously compounded** spot rates (stated in the BoE yield-curve FAQ). ECB: treated as **continuously compounded** (the ECB data portal labels the series this way; full documentation not yet read). Illustrative curves: the bond's coupon frequency. |
| Credit spread compounding | The spread is added to the zero rate and compounded the same way, so on gilt curves it is a continuously compounded spread. |
| Gilt curve | BoE nominal spot curve, 0.5–40 years every 6 months; the latest day with at least 20 maturities is used. Beyond 40 years the rate is held flat. |
| Treasury bootstrap | Semi-annual grid out to 30 years; par yields linearly interpolated between published maturities; at 6 months and shorter, zero = par (no intermediate coupons). Grid times are exact half-years, while the pricer measures time as ACT/365F, a difference of a few days at most. |
| Interpolation | Linear between curve points. |
| Long end | **Flat extrapolation** of the zero rate beyond the curve's last tenor (the price used everywhere). For the century bond on a 40-year gilt curve, about 8% of value depends on this. |
| Long-end sensitivity | Shown, not used for pricing. Every rule holds the forward rate constant past the last tenor and differs only in its level: the 40Y zero rate (current rule); the last-segment forward (39.5–40Y); the 30–40Y average forward; and the 40Y zero rate ±50/±100 bp. The offsets move the *forward*, not the zero rate, so the curve joins smoothly; a zero-rate jump at 40Y would re-discount a 40.5-year payment over its whole life and overstate the effect. On the real 29 Oct gilt curve the long end falls, so its forwards (3.28%, 4.20%) sit well below the 40Y zero rate (5.33%) and give prices 1.8–3.8% higher. The last-segment forward rests on two neighbouring points of the BoE's fitted curve and is noisy. |
| Discounting time | ACT/365F years from settlement to payment date. |
| Yield, duration, convexity | Street convention: periods counted with the bond's day count, compounded at coupon frequency. Matches QuantLib. |
| Payment dates | Rolled to the next weekday. Bank holidays are not modelled yet. |
| Accrual dates | Unadjusted (standard for bonds). |
| Settlement | Today in UTC by default; set `DEBTLAB_SETTLEMENT_DATE` to override. No T+1/T+2 settlement lag yet. |
| Stubs | Short first periods handled under ACT/ACT (ICMA) and 30/360. Long stubs aren't generated. A first period is regular when it starts exactly one period before its end *as measured from maturity*, so a bond issued 31 Aug and maturing 31 Aug has a regular first period ending 28 Feb. |
| Regular coupons | Every regular period pays exactly rate ÷ frequency (3.00 per 100 for 6% semi-annual), whatever the day count, as bond indentures specify. QuantLib's `FixedRateBond` on 30/360 instead sets month-end coupons by day count (2.9667, 3.05, …), so that case is not compared with QuantLib. 30/360 is bond basis with no end-of-February adjustment, so yield exponents of month-end 30/360 bonds differ from whole periods by a day or two. None of our instruments is month-end. |

## Data

| Item | Assumption |
|---|---|
| Source failures | Any failure loading a currency's curve (download, parse, bootstrap) is recorded with its type and falls back to that currency's last stored official curve; other currencies load normally. The dashboard banner lists each failure from the last refresh. In offline mode no source is contacted, and the banner says offline mode was chosen rather than that anything failed. |
| Bonds outside their life | A bond is not priced before its issue date or on/after maturity; it shows "not priced" with the reason and the other bonds are unaffected. |
| Freshness | A daily curve becomes **stale after 96 hours**, measured from midnight UTC on its observation date. That allows a weekend plus one late publication. |
| Illustrative GBP curve | 10Y and 30Y set near levels reported in the press on 14 Oct 2026 (10Y about 5.4%, 30Y about 6%). Every other point is invented for shape. |
| Illustrative USD curve | Entirely invented for a plausible shape. |
| Source formats | Treasury, ECB and BoE parsers verified against real downloads on 30 Oct 2026 (saved in `tests/fixtures/real/`). |

## Technical

| Item | Assumption |
|---|---|
| Database | SQLite (WAL mode) until deployment (M9). |
| Dashboard | Plain HTML, JS and SVG; fonts from Google Fonts with system fallbacks. |
| Precision | Decimal context at 40 significant digits. |
