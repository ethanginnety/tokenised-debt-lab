"""Fixed-rate bonds: terms, coupon periods and cash flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from debtlab.money import D
from debtlab.pricing.dates import (
    ACT_365F,
    SUPPORTED_DAY_COUNTS,
    accrual_fraction,
    add_months,
    coupon_schedule,
    following,
)


@dataclass(frozen=True)
class Period:
    """One coupon period. `ref_*` is the regular period used for ACT/ACT-ICMA."""

    start: date
    end: date
    ref_start: date
    ref_end: date
    pay_date: date
    coupon: Decimal
    principal: Decimal

    @property
    def regular(self) -> bool:
        return self.start == self.ref_start


@dataclass(frozen=True)
class FixedRateBond:
    """A plain fixed-coupon bond. Amounts are in the bond's own currency."""

    id: str
    name: str
    currency: str
    face: Decimal
    coupon_rate: Decimal  # annual, as a fraction
    frequency: int  # coupons per year
    issue_date: date
    maturity_date: date
    day_count: str
    benchmark: str  # which currency's government curve to use
    credit_spread: Decimal  # fraction over the benchmark, an ASSUMPTION
    description: str = ""
    assumptions: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.day_count not in SUPPORTED_DAY_COUNTS:
            raise ValueError(f"unsupported day count {self.day_count}")
        if self.face <= 0:
            raise ValueError("face value must be positive")

    def periods(self) -> list[Period]:
        dates = coupon_schedule(self.issue_date, self.maturity_date, self.frequency)
        step = 12 // self.frequency
        n = len(dates) - 1
        # The first period's notional regular start, measured from maturity like
        # every schedule date (not from its clamped end: 28 Feb - 6m is 28 Aug, not 31 Aug).
        first_ref_start = add_months(self.maturity_date, -step * n)
        out: list[Period] = []
        for i, (start, end) in enumerate(zip(dates, dates[1:])):
            ref_start = first_ref_start if i == 0 else start
            if self.day_count == ACT_365F or ref_start != start:
                # Day count decides the amount for stubs (and always for ACT/365F).
                frac = accrual_fraction(
                    self.day_count, start, end, ref_start, end, self.frequency
                )
                coupon = self.face * self.coupon_rate * frac
            else:
                # Regular periods pay exactly coupon / frequency.
                coupon = self.face * self.coupon_rate / D(self.frequency)
            principal = self.face if end == self.maturity_date else D(0)
            out.append(
                Period(start, end, ref_start, end, following(end), coupon, principal)
            )
        return out

    def remaining_periods(self, settle: date) -> list[Period]:
        """Periods whose payment is still owed to a buyer settling on `settle`."""
        if settle < self.issue_date or settle >= self.maturity_date:
            raise ValueError("settlement must fall between issue and maturity")
        return [p for p in self.periods() if p.end > settle]

    def accrued_interest(self, settle: date) -> Decimal:
        """Interest earned by the seller since the last coupon (per bond)."""
        current = self.remaining_periods(settle)[0]
        if settle <= current.start:
            return D(0)
        frac = accrual_fraction(
            self.day_count,
            current.start,
            settle,
            current.ref_start,
            current.ref_end,
            self.frequency,
        )
        return self.face * self.coupon_rate * frac
