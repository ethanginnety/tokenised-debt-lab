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
| Stubs | Short first periods handled under ACT/ACT (ICMA) and 30/360. Long stubs aren't generated. |

## Data

| Item | Assumption |
|---|---|
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
