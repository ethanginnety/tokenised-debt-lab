"""Turning par yields into zero rates ("bootstrapping").

The US Treasury publishes *par* yields: for each maturity, the coupon a
government bond would need to price at exactly 100. Pricing needs *zero*
rates instead: the rate for a single payment on one future date. The two
differ because a par bond pays coupons along the way, so its yield is an
average over many dates.

Bootstrapping recovers zero rates one maturity at a time, shortest first.
For a par bond maturing after k half-years with annual coupon c:

    100 = (c/2 × 100) × (DF_1 + ... + DF_k)  +  100 × DF_k

Every DF before DF_k is already known from earlier steps, so

    DF_k = (1 − c/2 × (DF_1 + ... + DF_{k−1})) / (1 + c/2)

and the semi-annual zero rate is z_k = 2 × (DF_k^(−1/k) − 1).

Conventions (see ASSUMPTIONS.md):
* Treasury notes and bonds pay semi-annually, so the grid is every 6 months.
* Par yields between published maturities are interpolated linearly.
* At 6 months and shorter there are no coupons in between, so zero = par.
* Grid times are exact half-years; the pricer measures time as ACT/365F.
"""

from __future__ import annotations

from decimal import Decimal

from debtlab.money import ONE, D
from debtlab.pricing.curve import YieldCurve

HALF = D("0.5")


def bootstrap_semiannual_par(par: YieldCurve) -> YieldCurve:
    """Zero curve (semi-annual compounding) from a semi-annual par curve."""
    if par.shift:
        raise ValueError("bootstrap the unshifted par curve, then shift the result")
    longest = par.longest_tenor
    steps = int(longest / HALF)
    if steps < 1:
        raise ValueError("par curve must reach at least 6 months")

    points: list[tuple[Decimal, Decimal]] = [
        (t, r) for t, r in par.points if t < HALF  # bills: single payment, zero = par
    ]
    dfs: list[Decimal] = []
    for k in range(1, steps + 1):
        t = HALF * k
        c = par.zero_rate(t)  # interpolated par yield at this maturity
        coupon = c / 2
        df = (ONE - coupon * sum(dfs, D(0))) / (ONE + coupon)
        if df <= 0:
            raise ArithmeticError(f"bootstrap produced a non-positive discount factor at {t}y")
        dfs.append(df)
        z = 2 * (df ** (-ONE / D(k)) - ONE)
        points.append((t, z))

    return YieldCurve(par.currency, tuple(points), compounding=2)


def par_yield_from_zero(zero: YieldCurve, maturity_halfyears: int) -> Decimal:
    """Inverse check: the par coupon implied by a zero curve at a grid maturity."""
    dfs = [
        (ONE + zero.zero_rate(HALF * i) / 2) ** (-D(i))
        for i in range(1, maturity_halfyears + 1)
    ]
    return 2 * (ONE - dfs[-1]) / sum(dfs, D(0))
