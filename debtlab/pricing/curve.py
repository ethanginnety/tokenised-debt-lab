"""Benchmark yield curves.

A yield curve says what interest rate the government pays for each length of
borrowing (its "tenor"). We price a corporate bond by discounting each of its
payments at the government rate for that date plus a credit spread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from debtlab.money import ONE, D, bp

CONTINUOUS = 0  # compounding code for continuously compounded rates


def discount_factor(rate: Decimal, t: Decimal, compounding: int) -> Decimal:
    """Value today of 1 paid in `t` years at `rate`.

    Continuous:  DF = e^(−r·t)
    f per year:  DF = (1 + r/f)^(−f·t)
    """
    if compounding == CONTINUOUS:
        return (-rate * t).exp()
    f = D(compounding)
    return (ONE + rate / f) ** (-f * t)


@dataclass(frozen=True)
class YieldCurve:
    """Zero rates by tenor, as fractions (0.045 = 4.5%).

    Interpolation is linear between points. Beyond the longest point the rate
    is held flat ("flat extrapolation"), which matters a lot for a 100-year
    bond because no government curve goes out that far.
    """

    currency: str
    points: tuple[tuple[Decimal, Decimal], ...]  # (tenor in years, rate)
    extrapolation: str = "flat"
    shift: Decimal = field(default=Decimal(0))
    # How the rates compound when turned into discount factors: 2 = semi-annual,
    # CONTINUOUS (0) = continuously. None means "use the bond's own coupon
    # frequency" (the M1 behaviour, kept for illustrative curves).
    compounding: int | None = None

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a curve needs at least two points")
        tenors = [t for t, _ in self.points]
        if tenors != sorted(tenors) or len(set(tenors)) != len(tenors):
            raise ValueError("curve tenors must be strictly increasing")
        if self.extrapolation != "flat":
            raise ValueError("only flat extrapolation is implemented")
        if self.compounding is not None and self.compounding not in (CONTINUOUS, 1, 2, 4, 12):
            raise ValueError(f"unsupported compounding {self.compounding}")

    @property
    def longest_tenor(self) -> Decimal:
        return self.points[-1][0]

    def zero_rate(self, t: Decimal) -> Decimal:
        pts = self.points
        if t <= pts[0][0]:
            rate = pts[0][1]
        elif t >= pts[-1][0]:
            rate = pts[-1][1]
        else:
            rate = pts[-1][1]
            for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
                if t0 <= t <= t1:
                    rate = r0 + (r1 - r0) * (t - t0) / (t1 - t0)
                    break
        return rate + self.shift

    def compounding_for(self, bond_frequency: int) -> int:
        return bond_frequency if self.compounding is None else self.compounding

    def shifted(self, shift_bp: object) -> "YieldCurve":
        """Same curve moved up or down in parallel by `shift_bp` basis points."""
        return YieldCurve(
            currency=self.currency,
            points=self.points,
            extrapolation=self.extrapolation,
            shift=self.shift + bp(shift_bp),
            compounding=self.compounding,
        )


_TENOR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mo|m|month|months|yr|y|year|years)\s*$", re.I)


def tenor_to_years(label: str) -> Decimal:
    """Parse labels like '1 Mo', '6M', '10 Yr', '30Y' into years."""
    m = _TENOR.match(label)
    if not m:
        raise ValueError(f"cannot parse tenor {label!r}")
    n = D(m.group(1))
    unit = m.group(2).lower()
    return n / D(12) if unit.startswith("m") else n


def curve_from_percent(currency: str, points: dict[str, object]) -> YieldCurve:
    """Build a curve from {'10Y': 4.25, ...} with rates given in percent."""
    parsed = sorted((tenor_to_years(k), D(v) / D(100)) for k, v in points.items())
    return YieldCurve(currency=currency, points=tuple(parsed))
