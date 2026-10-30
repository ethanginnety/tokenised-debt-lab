"""Benchmark yield curves.

A yield curve says what interest rate the government pays for each length of
borrowing (its "tenor"). We price a corporate bond by discounting each of its
payments at the government rate for that date plus a credit spread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal

from debtlab.money import ONE, D, bp

CONTINUOUS = 0  # compounding code for continuously compounded rates
EXTRAPOLATIONS = ("flat", "flat_forward")


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

    Interpolation is linear between points. Beyond the longest point a rule
    is needed, which matters a lot for a 100-year bond because no government
    curve goes out that far:
      "flat"          the zero rate is held at its last value (the default);
      "flat_forward"  the forward rate is held at its average between
                      `forward_from` (default: the second-to-last point) and
                      the last point, so zero rates keep drifting towards it.
    Both are "the forward rate beyond the last point is constant": flat zero
    is the special case where that forward equals the last zero rate.
    `long_end_shift` moves that forward up or down (sensitivity runs), so the
    curve still joins smoothly at the last point.
    """

    currency: str
    points: tuple[tuple[Decimal, Decimal], ...]  # (tenor in years, rate)
    extrapolation: str = "flat"
    shift: Decimal = field(default=Decimal(0))
    forward_from: Decimal | None = None
    long_end_shift: Decimal = field(default=Decimal(0))
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
        if self.compounding is not None and self.compounding not in (CONTINUOUS, 1, 2, 4, 12):
            raise ValueError(f"unsupported compounding {self.compounding}")
        if self.extrapolation not in EXTRAPOLATIONS:
            raise ValueError(f"unknown extrapolation {self.extrapolation!r}")
        if self.extrapolation == "flat_forward":
            if self.compounding is None:
                raise ValueError("flat_forward needs a curve that states its compounding")
            if self.forward_from is not None and not tenors[0] <= self.forward_from < tenors[-1]:
                raise ValueError("forward_from must lie inside the curve")

    @property
    def longest_tenor(self) -> Decimal:
        return self.points[-1][0]

    def zero_rate(self, t: Decimal) -> Decimal:
        if t > self.longest_tenor:
            return self._extrapolate(t) + self.shift
        return self._interpolate(t) + self.shift

    def _interpolate(self, t: Decimal) -> Decimal:
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
        return rate

    def _extrapolate(self, t: Decimal) -> Decimal:
        last_t, last_r = self.points[-1]
        if self.extrapolation == "flat" and not self.long_end_shift:
            return last_r
        # Constant forward beyond the last point, in continuous terms:
        #   r(t)·t = r_last·last + fwd·(t − last)
        c_last = self._to_cont(last_r)
        fwd = c_last if self.extrapolation == "flat" else self._end_forward_cont()
        fwd += self.long_end_shift
        return self._from_cont((c_last * last_t + fwd * (t - last_t)) / t)

    def _end_forward_cont(self) -> Decimal:
        """Continuous forward between `forward_from` and the last point.

        In continuous terms r(t)·t = ln(1/DF), so the forward between a and b
        is (r_b·b − r_a·a)/(b − a).
        """
        a = self.points[-2][0] if self.forward_from is None else self.forward_from
        last_t, last_r = self.points[-1]
        return (self._to_cont(last_r) * last_t - self._to_cont(self._interpolate(a)) * a) / (last_t - a)

    def _to_cont(self, r: Decimal) -> Decimal:
        """Continuously compounded equivalent: m·ln(1 + r/m)."""
        if self.compounding == CONTINUOUS:
            return r
        m = D(self.compounding)
        return m * (ONE + r / m).ln()

    def _from_cont(self, c: Decimal) -> Decimal:
        if self.compounding == CONTINUOUS:
            return c
        m = D(self.compounding)
        return m * ((c / m).exp() - ONE)

    def forward_at_end(self) -> Decimal:
        """The forward rate held beyond the last point, under this curve's rule."""
        c = self._to_cont(self.points[-1][1]) if self.extrapolation == "flat" else self._end_forward_cont()
        return self._from_cont(c + self.long_end_shift)

    def compounding_for(self, bond_frequency: int) -> int:
        return bond_frequency if self.compounding is None else self.compounding

    def shifted(self, shift_bp: object) -> "YieldCurve":
        """Same curve moved up or down in parallel by `shift_bp` basis points."""
        return replace(self, shift=self.shift + bp(shift_bp))

    def with_long_end(
        self, extrapolation: str, forward_from: Decimal | None = None, shift_bp: object = 0
    ) -> "YieldCurve":
        """Same published points, different rule beyond the last one."""
        return replace(
            self, extrapolation=extrapolation, forward_from=forward_from, long_end_shift=bp(shift_bp)
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
