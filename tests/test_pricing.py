"""Pricing tests: hand-checkable cases plus an independent QuantLib reference."""

from datetime import date
from decimal import Decimal

import pytest

from debtlab.money import D
from debtlab.pricing import engine
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.curve import YieldCurve, curve_from_percent, tenor_to_years
from debtlab.pricing.dates import (
    ACT_365F,
    ACT_ACT_ICMA,
    THIRTY_360,
    add_months,
    coupon_schedule,
    days_30_360,
    following,
)

ql = pytest.importorskip("QuantLib")

TOL = D("1e-9")


def make_bond(issue, mat, cpn, freq, dc, spread="0", ccy="GBP"):
    return FixedRateBond(
        id="t", name="t", currency=ccy, face=D(100), coupon_rate=D(cpn),
        frequency=freq, issue_date=issue, maturity_date=mat, day_count=dc,
        benchmark=ccy, credit_spread=D(spread),
    )


# ---------------------------------------------------------------- dates


def test_30_360_bond_basis():
    assert days_30_360(date(2026, 1, 31), date(2026, 3, 1)) == 31
    assert days_30_360(date(2026, 1, 30), date(2026, 3, 31)) == 60
    assert days_30_360(date(2026, 2, 28), date(2026, 8, 28)) == 180


def test_add_months_clamps_month_end():
    assert add_months(date(2026, 8, 31), -6) == date(2026, 2, 28)
    assert add_months(date(2028, 8, 31), -6) == date(2028, 2, 29)


def test_following_rolls_weekend():
    assert following(date(2026, 10, 31)) == date(2026, 11, 2)  # Saturday -> Monday
    assert following(date(2026, 10, 29)) == date(2026, 10, 29)


def test_schedule_century_bond_has_100_annual_periods():
    s = coupon_schedule(date(2026, 3, 30), date(2126, 3, 30), 1)
    assert len(s) == 101 and s[0] == date(2026, 3, 30) and s[-1] == date(2126, 3, 30)


def test_schedule_short_stub_at_start():
    s = coupon_schedule(date(2026, 1, 10), date(2027, 6, 10), 2)
    assert s == [date(2026, 1, 10), date(2026, 6, 10), date(2026, 12, 10), date(2027, 6, 10)]


# ---------------------------------------------------------------- hand checks


def test_par_bond_prices_at_par_on_coupon_date():
    b = make_bond(date(2020, 6, 1), date(2030, 6, 1), "0.05", 2, ACT_ACT_ICMA)
    assert abs(engine.clean_price_from_yield(b, D("0.05"), date(2026, 6, 1)) - 100) < TOL


def test_matches_closed_form_annuity():
    # 10 annual coupons of 6, yield 4%, on a coupon date: independent formula.
    b = make_bond(date(2026, 3, 1), date(2036, 3, 1), "0.06", 1, ACT_ACT_ICMA)
    y = D("0.04")
    closed = D(6) * (1 - (1 + y) ** -10) / y + D(100) * (1 + y) ** -10
    got = engine.clean_price_from_yield(b, y, date(2026, 3, 1))
    assert abs(got - closed) < TOL


def test_price_and_yield_round_trip():
    b = make_bond(date(2026, 3, 30), date(2126, 3, 30), "0.06", 1, ACT_ACT_ICMA)
    settle = date(2026, 10, 29)
    p = engine.clean_price_from_yield(b, D("0.0661"), settle)
    assert abs(engine.yield_from_clean_price(b, p, settle) - D("0.0661")) < D("1e-12")


def test_bond_price_falls_when_yield_rises():
    b = make_bond(date(2025, 1, 15), date(2035, 1, 15), "0.04", 2, ACT_ACT_ICMA)
    s = date(2026, 10, 29)
    assert engine.clean_price_from_yield(b, D("0.05"), s) < engine.clean_price_from_yield(b, D("0.04"), s)


def test_century_bond_duration_below_perpetuity_limit():
    # A perpetuity's modified duration is 1/y; a 100-year bond is just under it.
    b = make_bond(date(2026, 3, 30), date(2126, 3, 30), "0.06", 1, ACT_ACT_ICMA)
    r = engine.yield_risk(b, D("0.06"), date(2027, 3, 30))
    assert D(15) < r.modified_duration < D(1) / D("0.06")


# ---------------------------------------------------------------- QuantLib


def _ql(issue, mat, cpn, freq, dc, settle):
    qd = lambda d: ql.Date(d.day, d.month, d.year)  # noqa: E731
    ql.Settings.instance().evaluationDate = qd(settle)
    sched = ql.Schedule(
        qd(issue), qd(mat), ql.Period({1: ql.Annual, 2: ql.Semiannual}[freq]),
        ql.NullCalendar(), ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False,
    )
    qdc = (ql.ActualActual(ql.ActualActual.ISMA, sched) if dc == ACT_ACT_ICMA
           else ql.Thirty360(ql.Thirty360.BondBasis))
    return ql.FixedRateBond(0, 100.0, sched, [cpn], qdc), qdc, qd(settle)


QL_CASES = [
    (date(2020, 3, 15), date(2030, 3, 15), 0.05, 2, ACT_ACT_ICMA, date(2026, 10, 29), 0.0437),
    (date(2026, 3, 30), date(2126, 3, 30), 0.06, 1, ACT_ACT_ICMA, date(2026, 10, 29), 0.0661),
    (date(2025, 11, 12), date(2028, 11, 12), 0.045, 2, THIRTY_360, date(2026, 10, 29), 0.051),
    (date(2026, 1, 10), date(2031, 6, 10), 0.04, 2, ACT_ACT_ICMA, date(2026, 3, 2), 0.035),
    (date(2024, 2, 20), date(2054, 2, 20), 0.0475, 2, ACT_ACT_ICMA, date(2026, 12, 31), 0.058),
]


@pytest.mark.parametrize("issue,mat,cpn,freq,dc,settle,y", QL_CASES)
def test_against_quantlib(issue, mat, cpn, freq, dc, settle, y):
    b = make_bond(issue, mat, str(cpn), freq, dc)
    qb, qdc, qs = _ql(issue, mat, cpn, freq, dc, settle)
    fr = {1: ql.Annual, 2: ql.Semiannual}[freq]
    ref_clean = ql.BondFunctions.cleanPrice(qb, y, qdc, ql.Compounded, fr, qs)
    ref_acc = ql.BondFunctions.accruedAmount(qb, qs)
    ref_mod = ql.BondFunctions.duration(qb, y, qdc, ql.Compounded, fr, ql.Duration.Modified, qs)
    ref_mac = ql.BondFunctions.duration(qb, y, qdc, ql.Compounded, fr, ql.Duration.Macaulay, qs)
    ref_conv = ql.BondFunctions.convexity(qb, y, qdc, ql.Compounded, fr, qs)
    r = engine.yield_risk(b, D(str(y)), settle)
    assert abs(float(engine.clean_price_from_yield(b, D(str(y)), settle)) - ref_clean) < 1e-9
    assert abs(float(engine.accrued_per_100(b, settle)) - ref_acc) < 1e-9
    assert abs(float(r.modified_duration) - ref_mod) < 1e-9
    assert abs(float(r.macaulay_duration) - ref_mac) < 1e-9
    assert abs(float(r.convexity) - ref_conv) < 1e-6


# ---------------------------------------------------------------- curve pricing


def test_tenor_parsing():
    assert tenor_to_years("1 Mo") == D(1) / D(12)
    assert tenor_to_years("10 Yr") == D(10)
    assert tenor_to_years("30Y") == D(30)


def test_curve_interpolation_and_flat_extrapolation():
    c = curve_from_percent("GBP", {"1Y": 4, "10Y": 5, "50Y": 6})
    assert c.zero_rate(D("0.5")) == D("0.04")
    assert c.zero_rate(D("5.5")) == D("0.045")
    assert c.zero_rate(D(100)) == D("0.06")  # flat beyond 50 years
    assert c.shifted(25).zero_rate(D(100)) == D("0.0625")


def test_flat_curve_pricing_equals_yield_pricing_on_coupon_date():
    # With a flat curve, discounting on a coupon date with ACT/365F time differs
    # from period counting only by day-count noise; spread adds to the rate.
    b = make_bond(date(2026, 3, 2), date(2031, 3, 3), "0.05", 1, ACT_365F, spread="0.01")
    c = YieldCurve("GBP", ((D(1), D("0.04")), (D(30), D("0.04"))))
    fair = engine.dirty_price_from_curve(b, c, date(2026, 3, 2))
    assert abs(fair - engine.dirty_price_from_yield(b, D("0.05"), date(2026, 3, 2))) < D("0.05")


def test_valuation_shocks_have_right_signs_and_close_approximation():
    b = make_bond(date(2025, 6, 1), date(2035, 6, 1), "0.045", 2, ACT_ACT_ICMA, spread="0.012")
    c = curve_from_percent("GBP", {"1Y": 4, "5Y": 4.4, "10Y": 4.8, "30Y": 5.3})
    v = engine.value_bond(b, c, date(2026, 10, 29))
    s = {x.name: x for x in v.shocks}
    assert s["Benchmark +25 bp"].change_pct < 0 < s["Benchmark −25 bp"].change_pct
    assert s["Spread +200 bp"].change_pct < s["Spread +50 bp"].change_pct < 0
    assert abs(s["Benchmark +25 bp"].approx_error_pct) < D("0.01")
    assert v.fair_clean == v.fair_dirty - v.accrued


def test_century_bond_more_rate_sensitive_than_short_bond():
    c = curve_from_percent("GBP", {"1Y": 4, "10Y": 5, "30Y": 5.5, "50Y": 5.4})
    century = make_bond(date(2026, 3, 30), date(2126, 3, 30), "0.06", 1, ACT_ACT_ICMA, "0.009")
    short = make_bond(date(2026, 3, 30), date(2029, 3, 30), "0.045", 2, ACT_ACT_ICMA, "0.012")
    vc = engine.value_bond(century, c, date(2026, 10, 29))
    vs = engine.value_bond(short, c, date(2026, 10, 29))
    up_c = next(x for x in vc.shocks if x.name == "Benchmark +100 bp").change_pct
    up_s = next(x for x in vs.shocks if x.name == "Benchmark +100 bp").change_pct
    assert up_c < 5 * up_s  # both negative; century loses at least 5x more
    # At ~6% rates only a few percent of value sits beyond the 50-year point,
    # but that slice depends entirely on the extrapolation assumption.
    assert D("0.02") < vc.beyond_curve_share < D("0.10")


def test_wrong_currency_curve_rejected():
    b = make_bond(date(2026, 3, 30), date(2029, 3, 30), "0.045", 2, ACT_ACT_ICMA)
    with pytest.raises(ValueError):
        engine.value_bond(b, curve_from_percent("USD", {"1Y": 4, "10Y": 4.5}), date(2026, 10, 29))


def test_settlement_outside_life_rejected():
    b = make_bond(date(2026, 3, 30), date(2029, 3, 30), "0.045", 2, ACT_ACT_ICMA)
    with pytest.raises(ValueError):
        b.accrued_interest(date(2030, 1, 1))
