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

## Pricing

| Item | Assumption |
|---|---|
| Curve rates | Treated as zero rates compounded at each bond's coupon frequency. In online mode, US Treasury **par** yields are used directly until the M2 bootstrap; that's a known approximation. |
| Interpolation | Linear between curve points. |
| Long end | **Flat extrapolation** beyond the curve's last tenor. For the century bond about 4% of value (on the illustrative curve) depends on this. |
| Discounting time | ACT/365F years from settlement to payment date. |
| Yield, duration, convexity | Street convention: periods counted with the bond's day count, compounded at coupon frequency. Matches QuantLib. |
| Payment dates | Rolled to the next weekday. Bank holidays are not modelled yet. |
| Accrual dates | Unadjusted (standard for bonds). |
| Settlement | Today in UTC by default; set `DEBTLAB_SETTLEMENT_DATE` to override. No T+1/T+2 settlement lag yet. |
| Stubs | Short first periods handled under ACT/ACT (ICMA) and 30/360. Long stubs aren't generated. |

## Data

| Item | Assumption |
|---|---|
| Freshness | A daily curve becomes **stale after 96 hours**, measured from midnight UTC on its observation date. That allows a weekend plus one late publication. |
| Illustrative GBP curve | 10Y and 30Y set near levels reported in the press on 14 Oct 2026 (10Y about 5.4%, 30Y about 6%). Every other point is invented for shape. |
| Illustrative USD curve | Entirely invented for a plausible shape. |
| Source formats | The Treasury and ECB parsers follow documented formats but **have not yet been run against a real download** (the build environment had no access). To be verified in M2. |

## Technical

| Item | Assumption |
|---|---|
| Database | SQLite (WAL mode) until deployment (M9). |
| Dashboard | Plain HTML, JS and SVG; fonts from Google Fonts with system fallbacks. |
| Precision | Decimal context at 40 significant digits. |
