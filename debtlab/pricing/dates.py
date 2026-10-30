"""Day counts, business days and coupon schedules.

Day-count conventions decide how much interest has built up between two dates.
Different markets use different rules, so every instrument names its own.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from debtlab.money import D

ACT_ACT_ICMA = "ACT/ACT-ICMA"  # government bonds, most sterling and euro bonds
THIRTY_360 = "30/360"  # US corporate bonds (ISDA "bond basis")
ACT_365F = "ACT/365F"  # used here to measure time for discounting

SUPPORTED_DAY_COUNTS = (ACT_ACT_ICMA, THIRTY_360, ACT_365F)


def days_30_360(d1: date, d2: date) -> int:
    """30/360 bond basis: every month counts as 30 days, every year as 360."""
    day1 = min(d1.day, 30)
    day2 = 30 if (d2.day == 31 and day1 == 30) else d2.day
    return 360 * (d2.year - d1.year) + 30 * (d2.month - d1.month) + (day2 - day1)


def act_365f(d1: date, d2: date) -> Decimal:
    """Actual days divided by 365, used as 'years' when discounting."""
    return D((d2 - d1).days) / D(365)


def add_months(d: date, months: int) -> date:
    """Move by whole months, clamping to the last day of shorter months."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def is_business_day(d: date) -> bool:
    """Weekends only. Bank holidays are not modelled yet (see ASSUMPTIONS.md)."""
    return d.weekday() < 5


def following(d: date) -> date:
    """Roll a payment date forward to the next business day."""
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def business_days_before(d: date, n: int) -> date:
    """The business day `n` business days before `d` (weekends skipped)."""
    while n > 0:
        d -= timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d


def coupon_schedule(issue: date, maturity: date, frequency: int) -> list[date]:
    """Accrual dates from issue to maturity, generated backwards from maturity.

    Backwards generation keeps every coupon on the same day of the month as
    maturity; any odd-length (stub) period lands at the start. Each date is
    computed directly from maturity, so month-end clamping never drifts.
    """
    if frequency not in (1, 2, 4, 12):
        raise ValueError(f"unsupported coupon frequency {frequency}")
    if maturity <= issue:
        raise ValueError("maturity must be after issue")
    step = 12 // frequency
    dates = [maturity]
    k = 1
    while True:
        d = add_months(maturity, -step * k)
        if d <= issue:
            break
        dates.append(d)
        k += 1
    dates.append(issue)
    return sorted(dates)


def accrual_fraction(
    day_count: str,
    start: date,
    end: date,
    ref_start: date,
    ref_end: date,
    frequency: int,
) -> Decimal:
    """Fraction of one year's coupon earned between `start` and `end`.

    For ACT/ACT-ICMA the fraction is measured against the reference coupon
    period (`ref_start` to `ref_end`). For a short first period, `ref_start` is
    the notional regular start date, so the stub earns proportionally less.
    """
    if day_count == ACT_ACT_ICMA:
        period_days = D((ref_end - ref_start).days)
        return D((end - start).days) / period_days / D(frequency)
    if day_count == THIRTY_360:
        return D(days_30_360(start, end)) / D(360)
    if day_count == ACT_365F:
        return act_365f(start, end)
    raise ValueError(f"unsupported day count {day_count}")
