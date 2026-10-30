"""Bond valuation: prices, yields and interest-rate risk.

Two ways to price, and both are needed:

* From the curve (fair value): discount each payment at
  (government zero rate for that date + credit spread). This is the model's
  view of what the bond is worth.
* From a single yield (yield to maturity): the one rate that, used for every
  payment, reproduces a given price. Traders quote bonds this way, and
  duration and convexity are defined from it.

All prices returned here are per 100 of face value, the market convention.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from debtlab.money import HUNDRED, ONE, ZERO, D, bp
from debtlab.pricing.bond import FixedRateBond, Period
from debtlab.pricing.curve import YieldCurve, discount_factor
from debtlab.pricing.dates import accrual_fraction, act_365f


# --------------------------------------------------------------------------
# Pricing from the curve
# --------------------------------------------------------------------------


def _per_100(bond: FixedRateBond, amount: Decimal) -> Decimal:
    return amount / bond.face * HUNDRED


def dirty_price_from_curve(
    bond: FixedRateBond,
    curve: YieldCurve,
    settle: date,
    spread_shift_bp: object = 0,
) -> Decimal:
    """Present value of all remaining payments, per 100 face.

    Each payment at time t (years, ACT/365F from settlement to payment date) is
    discounted at r = zero(t) + spread, compounded f times a year:
        DF = (1 + r / f) ** (-f * t)
    where f is the curve's own compounding if it states one, else the bond's
    coupon frequency. Continuously compounded curves use DF = e^(−r·t).
    """
    pv = sum((v for _, v in _cash_flow_pvs(bond, curve, settle, spread_shift_bp)), ZERO)
    return _per_100(bond, pv)


def _cash_flow_pvs(
    bond: FixedRateBond, curve: YieldCurve, settle: date, spread_shift_bp: object = 0
) -> list[tuple[Decimal, Decimal]]:
    """(time in years, present value) of each remaining payment: the one discounting rule."""
    comp = curve.compounding_for(bond.frequency)
    spread = bond.credit_spread + bp(spread_shift_bp)
    out = []
    for p in bond.remaining_periods(settle):
        t = act_365f(settle, p.pay_date)
        r = curve.zero_rate(t) + spread
        out.append((t, (p.coupon + p.principal) * discount_factor(r, t, comp)))
    return out


def accrued_per_100(bond: FixedRateBond, settle: date) -> Decimal:
    return _per_100(bond, bond.accrued_interest(settle))


# --------------------------------------------------------------------------
# Pricing from a single yield (street convention)
# --------------------------------------------------------------------------


def _period_exponents(
    bond: FixedRateBond, settle: date
) -> list[tuple[Decimal, Period]]:
    """Time to each payment measured in coupon periods.

    The first payment is `w` periods away, where w is the unexpired fraction of
    the current period by the bond's day count; later payments add one
    period's day-count fraction each (exactly 1 for regular periods).
    """
    f = D(bond.frequency)
    periods = bond.remaining_periods(settle)
    first = periods[0]
    start = max(settle, first.start)
    w = accrual_fraction(
        bond.day_count, start, first.end, first.ref_start, first.ref_end, bond.frequency
    ) * f
    out = [(w, first)]
    acc = w
    for p in periods[1:]:
        acc += accrual_fraction(
            bond.day_count, p.start, p.end, p.ref_start, p.ref_end, bond.frequency
        ) * f
        out.append((acc, p))
    return out


def dirty_price_from_yield(bond: FixedRateBond, ytm: Decimal, settle: date) -> Decimal:
    f = D(bond.frequency)
    base = ONE + ytm / f
    pv = sum(
        ((p.coupon + p.principal) * base ** (-n) for n, p in _period_exponents(bond, settle)),
        ZERO,
    )
    return _per_100(bond, pv)


def clean_price_from_yield(bond: FixedRateBond, ytm: Decimal, settle: date) -> Decimal:
    return dirty_price_from_yield(bond, ytm, settle) - accrued_per_100(bond, settle)


def yield_from_clean_price(
    bond: FixedRateBond, clean_price: Decimal, settle: date, tol: Decimal = D("1e-14")
) -> Decimal:
    """Solve for yield to maturity with Newton's method, falling back to bisection."""
    target = clean_price + accrued_per_100(bond, settle)
    f = D(bond.frequency)
    y = bond.coupon_rate if bond.coupon_rate > 0 else D("0.05")
    for _ in range(100):
        base = ONE + y / f
        price = ZERO
        deriv = ZERO
        for n, p in _period_exponents(bond, settle):
            cf = _per_100(bond, p.coupon + p.principal)
            price += cf * base ** (-n)
            deriv += -n / f * cf * base ** (-n - 1)
        diff = price - target
        if abs(diff) < tol:
            return y
        if deriv == 0:
            break
        step = diff / deriv
        y_new = y - step
        if y_new <= -f:  # Newton overshot below the domain; use bisection
            break
        y = y_new
    lo, hi = D("-0.5"), D("2")
    for _ in range(300):
        mid = (lo + hi) / 2
        if dirty_price_from_yield(bond, mid, settle) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            return mid
    raise ArithmeticError("yield did not converge")


@dataclass(frozen=True)
class YieldRisk:
    macaulay_duration: Decimal  # years
    modified_duration: Decimal  # % price change per 1.00 change in yield
    convexity: Decimal
    dv01: Decimal  # price change per 100 face for a 1 bp yield move


def yield_risk(bond: FixedRateBond, ytm: Decimal, settle: date) -> YieldRisk:
    f = D(bond.frequency)
    base = ONE + ytm / f
    price = ZERO
    weighted_t = ZERO
    weighted_c = ZERO
    for n, p in _period_exponents(bond, settle):
        pv = _per_100(bond, p.coupon + p.principal) * base ** (-n)
        price += pv
        weighted_t += (n / f) * pv
        weighted_c += (n * (n + 1) / (f * f)) * pv
    mac = weighted_t / price
    mod = mac / base
    conv = weighted_c / (price * base * base)
    return YieldRisk(mac, mod, conv, mod * price * bp(1))


# --------------------------------------------------------------------------
# Full valuation and shock scenarios
# --------------------------------------------------------------------------

DEFAULT_SHOCKS: tuple[tuple[str, str, int], ...] = (
    ("Benchmark +25 bp", "benchmark", 25),
    ("Benchmark +100 bp", "benchmark", 100),
    ("Benchmark −25 bp", "benchmark", -25),
    ("Spread +50 bp", "spread", 50),
    ("Spread +200 bp", "spread", 200),
)


@dataclass(frozen=True)
class Shock:
    name: str
    kind: str
    shift_bp: int
    clean_price: Decimal
    change_pct: Decimal  # full repricing, % of base dirty price
    approx_change_pct: Decimal  # duration + convexity estimate
    approx_error_pct: Decimal


@dataclass(frozen=True)
class LongEndCase:
    name: str
    rule: str  # how rates beyond the curve's last tenor are set, in words
    rate_at_maturity: Decimal  # benchmark zero rate the rule gives at maturity
    clean_price: Decimal
    change: Decimal  # vs the current rule, per 100 face
    change_pct: Decimal  # vs the current rule, % of its clean price


# Every rule holds the forward rate constant beyond the curve's last tenor; they
# differ only in its level. (name, extrapolation, years before the last tenor
# the forward is measured from (None = last segment), shift to that forward in bp)
LONG_END_RULES: tuple[tuple[str, str, int | None, int], ...] = (
    ("Flat zero rate (current rule)", "flat", None, 0),
    ("Flat forward, last segment", "flat_forward", None, 0),
    ("Flat forward, last 10 years", "flat_forward", 10, 0),
    ("Long-run forward −100 bp", "flat", None, -100),
    ("Long-run forward −50 bp", "flat", None, -50),
    ("Long-run forward +50 bp", "flat", None, 50),
    ("Long-run forward +100 bp", "flat", None, 100),
)


def _yrs(t: Decimal) -> str:
    return format(t.normalize(), "f")  # 40.0 -> "40", 39.5 -> "39.5"


def long_end_sensitivity(
    bond: FixedRateBond, curve: YieldCurve, settle: date
) -> tuple[LongEndCase, ...]:
    """Reprice under each long-end rule. Empty if no payment is past the curve.

    Only the rule beyond the last published tenor changes; every published
    point is the same, so the differences are pure model risk.
    """
    years = act_365f(settle, bond.maturity_date)
    last = curve.longest_tenor
    if years <= last:
        return ()
    # Pin "use the bond's frequency" so flat_forward knows the convention; no DF changes.
    base = replace(curve, compounding=curve.compounding_for(bond.frequency))
    acc = accrued_per_100(bond, settle)
    base_clean = dirty_price_from_curve(bond, base.with_long_end("flat"), settle) - acc
    out = []
    for name, rule, window, shift in LONG_END_RULES:
        start = None if window is None else last - D(window)
        if start is not None and start < curve.points[0][0]:
            continue
        c = base.with_long_end(rule, forward_from=start, shift_bp=shift)
        if rule == "flat" and not shift:
            words = f"zero rate held at its {_yrs(last)}Y level"
        elif rule == "flat":
            words = f"forward beyond {_yrs(last)}Y = {_yrs(last)}Y zero rate {shift:+d} bp"
        else:
            span = f"{_yrs(curve.points[-2][0] if start is None else start)}–{_yrs(last)}Y"
            words = f"forward beyond {_yrs(last)}Y held at its {span} level"
        words += f" (forward {c.forward_at_end() * HUNDRED:.2f}%)"
        clean = dirty_price_from_curve(bond, c, settle) - acc
        out.append(LongEndCase(
            name, words, c.zero_rate(years), clean, clean - base_clean,
            (clean - base_clean) / base_clean * HUNDRED,
        ))
    return tuple(out)


@dataclass(frozen=True)
class Valuation:
    instrument_id: str
    settle: date
    fair_dirty: Decimal
    accrued: Decimal
    fair_clean: Decimal
    ytm: Decimal
    benchmark_rate_at_maturity: Decimal
    risk: YieldRisk
    effective_duration: Decimal
    effective_convexity: Decimal
    cash_flows_remaining: int
    years_to_maturity: Decimal
    beyond_curve_share: Decimal  # share of PV from payments past the curve's last tenor
    shocks: tuple[Shock, ...]
    long_end: tuple[LongEndCase, ...] = ()


def value_bond(
    bond: FixedRateBond,
    curve: YieldCurve,
    settle: date,
    shocks: tuple[tuple[str, str, int], ...] = DEFAULT_SHOCKS,
) -> Valuation:
    if curve.currency != bond.benchmark:
        raise ValueError(
            f"{bond.id} needs a {bond.benchmark} curve, got {curve.currency}"
        )
    dirty = dirty_price_from_curve(bond, curve, settle)
    accrued = accrued_per_100(bond, settle)
    clean = dirty - accrued
    ytm = yield_from_clean_price(bond, clean, settle)
    risk = yield_risk(bond, ytm, settle)

    # Effective duration/convexity: reprice with the whole curve 1 bp up/down.
    h = 1
    up = dirty_price_from_curve(bond, curve.shifted(h), settle)
    down = dirty_price_from_curve(bond, curve.shifted(-h), settle)
    dy = bp(h)
    eff_dur = (down - up) / (2 * dirty * dy)
    eff_conv = (down + up - 2 * dirty) / (dirty * dy * dy)

    # How much of today's value depends on extrapolated rates?
    pvs = _cash_flow_pvs(bond, curve, settle)
    total = sum((pv for _, pv in pvs), ZERO)
    beyond = sum((pv for t, pv in pvs if t > curve.longest_tenor), ZERO)

    results = []
    for name, kind, shift in shocks:
        if kind == "benchmark":
            shocked = dirty_price_from_curve(bond, curve.shifted(shift), settle)
        elif kind == "spread":
            shocked = dirty_price_from_curve(bond, curve, settle, spread_shift_bp=shift)
        else:
            raise ValueError(f"unknown shock kind {kind}")
        change = (shocked - dirty) / dirty * HUNDRED
        d_y = bp(shift)
        approx = (-eff_dur * d_y + eff_conv * d_y * d_y / 2) * HUNDRED
        results.append(
            Shock(name, kind, shift, shocked - accrued, change, approx, change - approx)
        )

    years = act_365f(settle, bond.maturity_date)
    return Valuation(
        instrument_id=bond.id,
        settle=settle,
        fair_dirty=dirty,
        accrued=accrued,
        fair_clean=clean,
        ytm=ytm,
        benchmark_rate_at_maturity=curve.zero_rate(years),
        risk=risk,
        effective_duration=eff_dur,
        effective_convexity=eff_conv,
        cash_flows_remaining=len(bond.remaining_periods(settle)),
        years_to_maturity=years,
        beyond_curve_share=beyond / total if total else ZERO,
        shocks=tuple(results),
        long_end=long_end_sensitivity(bond, curve, settle),
    )


def price_yield_profile(
    bond: FixedRateBond, ytm: Decimal, settle: date, span_bp: int = 300, step_bp: int = 25
) -> list[tuple[Decimal, Decimal]]:
    """(yield, clean price) pairs around the current yield, for charting convexity."""
    out = []
    for k in range(-span_bp, span_bp + 1, step_bp):
        y = ytm + bp(k)
        if y <= D("-0.5"):
            continue
        out.append((y, clean_price_from_yield(bond, y, settle)))
    return out
