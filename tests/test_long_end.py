"""Long-end extrapolation: the rules beyond a curve's last tenor, and the
century bond's sensitivity to them."""

from datetime import date
from pathlib import Path

import pytest

from debtlab.instruments import load_instruments
from debtlab.money import D
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.curve import CONTINUOUS, YieldCurve, discount_factor
from debtlab.pricing.dates import ACT_ACT_ICMA
from debtlab.pricing.engine import dirty_price_from_curve, long_end_sensitivity, value_bond

ROOT = Path(__file__).resolve().parent.parent
SETTLE = date(2026, 10, 30)
# Falls at the long end, like the real gilt curve (TEST data, not market data).
INVERTED = ((D("0.5"), D("0.042")), (D(20), D("0.057")), (D("39.5"), D("0.0535")), (D(40), D("0.0533")))


def century() -> FixedRateBond:
    return next(b for b in load_instruments(ROOT / "config" / "instruments.json") if b.currency == "GBP")


def test_flat_forward_hand_example():
    # Continuous: forward 10->20y = (5%·20 − 4%·10)/10 = 6%.
    # At 30y: r = (5%·20 + 6%·10)/30 = 1.6/30.
    c = YieldCurve("X", ((D(10), D("0.04")), (D(20), D("0.05"))), compounding=CONTINUOUS,
                   extrapolation="flat_forward")
    assert c.zero_rate(D(30)) == D("1.6") / D(30)
    assert c.forward_at_end() == D("0.06")
    assert c.zero_rate(D(15)) == D("0.045")  # inside the curve nothing changes


def test_flat_forward_on_semiannual_curve_keeps_forward_constant():
    c = YieldCurve("X", ((D(10), D("0.04")), (D(30), D("0.05"))), compounding=2,
                   extrapolation="flat_forward", forward_from=D(20))
    df = lambda t: discount_factor(c.zero_rate(D(t)), D(t), 2)  # noqa: E731
    f1 = (df(40) / df(50)).ln() / 10
    f2 = (df(60) / df(90)).ln() / 30
    f_curve = (df(20) / df(30)).ln() / 10
    assert abs(f1 - f_curve) < D("1e-30") and abs(f2 - f_curve) < D("1e-30")


def test_flat_is_flat_forward_at_the_last_zero_rate():
    c = YieldCurve("X", INVERTED, compounding=CONTINUOUS)
    assert c.zero_rate(D(90)) == D("0.0533")
    assert c.forward_at_end() == D("0.0533")


def test_long_end_shift_moves_the_forward_and_joins_smoothly():
    base = YieldCurve("X", INVERTED, compounding=CONTINUOUS)
    up = base.with_long_end("flat", shift_bp=100)
    assert up.forward_at_end() == D("0.0633")
    assert up.zero_rate(D(40)) == D("0.0533")  # published points untouched
    assert abs(up.zero_rate(D("40.01")) - D("0.0533")) < D("0.00001")  # no jump at 40y
    assert up.zero_rate(D(100)) == (D("0.0533") * 40 + D("0.0633") * 60) / 100


def test_parallel_shift_keeps_the_long_end_rule():
    c = YieldCurve("X", INVERTED, compounding=CONTINUOUS).with_long_end("flat_forward")
    s = c.shifted(25)
    assert s.extrapolation == "flat_forward"
    assert s.zero_rate(D(80)) - c.zero_rate(D(80)) == D("0.0025")


def test_invalid_long_end_settings_raise():
    with pytest.raises(ValueError):
        YieldCurve("X", INVERTED, extrapolation="flat_forward")  # compounding unknown
    with pytest.raises(ValueError):
        YieldCurve("X", INVERTED, compounding=CONTINUOUS, extrapolation="flat_forward", forward_from=D(40))
    with pytest.raises(ValueError):
        YieldCurve("X", INVERTED, compounding=CONTINUOUS, extrapolation="cubic")


def test_flat_forward_matches_quantlib_loglinear_extrapolation():
    """QuantLib's log-linear discount curve, extrapolated, continues the last
    segment's forward: the same rule as flat_forward with the default window."""
    ql = pytest.importorskip("QuantLib")
    # Whole-year pillars fall on exact day counts under ACT/365F.
    pts = ((D(1), D("0.042")), (D(20), D("0.057")), (D(39), D("0.0538")), (D(40), D("0.0533")))
    curve = YieldCurve("GBP", pts, compounding=CONTINUOUS, extrapolation="flat_forward")

    qd = lambda d: ql.Date(d.day, d.month, d.year)  # noqa: E731
    ql.Settings.instance().evaluationDate = qd(SETTLE)
    dates = [qd(SETTLE)] + [qd(SETTLE) + int(t) * 365 for t, _ in pts]
    dfs = [1.0] + [float((-r * t).exp()) for t, r in pts]
    ts = ql.DiscountCurve(dates, dfs, ql.Actual365Fixed())  # log-linear in DF
    ts.enableExtrapolation()
    checked = 0
    for p in century().remaining_periods(SETTLE):
        t = D((p.pay_date - SETTLE).days) / D(365)
        if t > D(40):
            mine = discount_factor(curve.zero_rate(t), t, CONTINUOUS)
            assert abs(float(mine) - ts.discount(qd(p.pay_date))) < 1e-14
            checked += 1
    assert checked == 60  # payments from Mar 2067 to Mar 2126


def test_sensitivity_table_on_century_bond():
    bond = century()
    curve = YieldCurve("GBP", INVERTED, compounding=CONTINUOUS)
    v = value_bond(bond, curve, SETTLE)
    rows = {r.name: r for r in v.long_end}
    base = rows["Flat zero rate (current rule)"]
    assert base.change == 0 and base.clean_price == v.fair_clean
    # A falling long end puts the forward below the 40y zero rate, so flat
    # forward discounts the far cash flows less and the price is higher.
    assert rows["Flat forward, last segment"].clean_price > base.clean_price
    # Higher long-run forward -> lower price, without exception.
    by_fwd = sorted(v.long_end, key=lambda r: r.rate_at_maturity)
    prices = [r.clean_price for r in by_fwd]
    assert prices == sorted(prices, reverse=True)
    # ±50 bp on the forward beyond 40y moves the price by well under ±1%:
    # small next to a 50 bp move in the whole curve (about 7%).
    assert D("-1") < rows["Long-run forward +50 bp"].change_pct < D(0)


def test_no_table_when_the_curve_covers_the_bond():
    short = next(b for b in load_instruments(ROOT / "config" / "instruments.json") if b.currency == "USD")
    usd = YieldCurve("USD", ((D("0.5"), D("0.04")), (D(30), D("0.05"))), compounding=2)
    assert long_end_sensitivity(short, usd, SETTLE) == ()


def test_table_works_on_curve_without_stated_compounding():
    # Illustrative curves leave compounding to the bond; the table must still run.
    curve = YieldCurve("GBP", INVERTED)
    rows = long_end_sensitivity(century(), curve, SETTLE)
    assert len(rows) == 7
    assert rows[0].clean_price == value_bond(century(), curve, SETTLE).fair_clean
