"""Bootstrapping par yields into zero rates."""

from datetime import date
from decimal import Decimal

import pytest

from debtlab.money import D
from debtlab.pricing.bootstrap import HALF, bootstrap_semiannual_par, par_yield_from_zero
from debtlab.pricing.curve import YieldCurve, curve_from_percent

# The real Treasury par curve for 29 Oct 2026, as downloaded on Ethan's machine.
REAL_2026_10_29 = {
    "1 Mo": 4.06, "1.5 Month": 4.08, "2 Mo": 4.17, "3 Mo": 4.18, "4 Mo": 4.21,
    "6 Mo": 4.28, "1 Yr": 4.42, "2 Yr": 4.61, "3 Yr": 4.73, "5 Yr": 4.78,
    "7 Yr": 4.87, "10 Yr": 4.98, "20 Yr": 5.35, "30 Yr": 5.32,
}


@pytest.fixture
def par():
    return curve_from_percent("USD", REAL_2026_10_29)


def test_flat_par_curve_gives_same_flat_zero_curve():
    flat = YieldCurve("USD", ((D("0.5"), D("0.05")), (D(30), D("0.05"))))
    zero = bootstrap_semiannual_par(flat)
    for _, z in zero.points:
        assert abs(z - D("0.05")) < D("1e-25")


def test_two_step_hand_example():
    # 6m par 4% -> DF1 = 1/1.02. 1y par 5% -> DF2 = (1 - 0.025*DF1)/1.025.
    zero = bootstrap_semiannual_par(YieldCurve("USD", ((D("0.5"), D("0.04")), (D(1), D("0.05")))))
    df1 = 1 / D("1.02")
    df2 = (1 - D("0.025") * df1) / D("1.025")
    assert zero.zero_rate(D("0.5")) == pytest.approx(D("0.04"), abs=D("1e-25"))
    assert abs(zero.zero_rate(D(1)) - 2 * (df2 ** (-D(1) / 2) - 1)) < D("1e-25")
    assert zero.zero_rate(D(1)) > D("0.05")  # upward-sloping: zero above par


def test_every_par_bond_reprices_to_par(par):
    zero = bootstrap_semiannual_par(par)
    assert zero.compounding == 2
    for k in range(1, 61):
        implied = par_yield_from_zero(zero, k)
        assert abs(implied - par.zero_rate(HALF * k)) < D("1e-24"), k


def test_real_curve_shape(par):
    zero = bootstrap_semiannual_par(par)
    # Mostly upward-sloping, so zero rates sit above par yields at 10Y and 20Y...
    assert zero.zero_rate(D(10)) > D("0.0498")
    assert zero.zero_rate(D(20)) > D("0.0535")
    # ...and the 20Y-to-30Y dip in par (5.35% -> 5.32%) shows up as the zero
    # curve turning down, more steeply than par does.
    assert zero.zero_rate(D(30)) < zero.zero_rate(D(20))
    assert zero.zero_rate(D(20)) - zero.zero_rate(D(30)) > D("0.0003")


def test_shifted_input_rejected(par):
    with pytest.raises(ValueError):
        bootstrap_semiannual_par(par.shifted(10))


def test_matches_quantlib_bootstrap(par):
    ql = pytest.importorskip("QuantLib")
    zero = bootstrap_semiannual_par(par)
    today = ql.Date(30, 10, 2026)
    ql.Settings.instance().evaluationDate = today
    dc = ql.Thirty360(ql.Thirty360.BondBasis)  # makes every 6-month period exactly 0.5
    helpers = []
    for k in range(1, 61):
        mat = today + ql.Period(6 * k, ql.Months)
        sched = ql.Schedule(today, mat, ql.Period(ql.Semiannual), ql.NullCalendar(),
                            ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False)
        c = float(par.zero_rate(HALF * k))
        helpers.append(ql.FixedRateBondHelper(ql.QuoteHandle(ql.SimpleQuote(100.0)), 0, 100.0,
                                              sched, [c], dc))
    curve = ql.PiecewiseLogLinearDiscount(today, helpers, dc)
    for k in (1, 2, 4, 10, 20, 40, 60):
        mat = today + ql.Period(6 * k, ql.Months)
        ref = curve.discount(mat)
        mine = float((1 + zero.zero_rate(HALF * k) / 2) ** (-Decimal(k)))
        assert abs(mine - ref) < 1e-12, k


def test_treasury_snapshot_is_bootstrapped():
    from datetime import datetime, timezone
    from pathlib import Path

    from debtlab.marketdata.adapters import parse_treasury_csv, to_zero_snapshot

    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    raw = parse_treasury_csv(
        (Path(__file__).parent / "fixtures" / "treasury_sample.csv").read_text(encoding="utf-8"), now
    )
    z = to_zero_snapshot(raw)
    assert z.rate_type == "zero_bootstrapped" and z.curve.compounding == 2
    assert z.observation_date == raw.observation_date == date(2026, 10, 27)
    assert z.curve.longest_tenor == D(30)
    assert to_zero_snapshot(z) is z  # already zero: unchanged
