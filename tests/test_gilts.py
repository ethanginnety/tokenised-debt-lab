"""Bank of England gilt curve parsing, continuous compounding, century bond pricing."""

import io
import math
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from debtlab.instruments import load_instruments
from debtlab.marketdata.adapters import BOE_FILE, BOE_SHEET, MarketDataError, parse_boe_zip
from debtlab.money import D
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.curve import CONTINUOUS, YieldCurve, discount_factor
from debtlab.pricing.dates import ACT_ACT_ICMA
from debtlab.pricing.engine import dirty_price_from_curve, value_bond

openpyxl = pytest.importorskip("openpyxl")

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent

# Values read from the real 29 Oct 2026 file in Ethan's probe run: the first
# seven maturities and the last four. Points in between are filled by a smooth
# test shape here (this is TEST data, not market data).
KNOWN = {0.5: 4.2379, 1.0: 4.4586, 1.5: 4.6144, 2.0: 4.7047, 2.5: 4.7596, 3.0: 4.7974,
         3.5: 4.8276, 38.5: 5.4031, 39.0: 5.3785, 39.5: 5.3533, 40.0: 5.3273}


def sample_rate(t: float) -> float:
    if t in KNOWN:
        return KNOWN[t]
    if t < 20:  # rise from 4.83% at 3.5y to 5.70% at 20y
        return 4.8276 + (5.70 - 4.8276) * (t - 3.5) / 16.5
    return 5.70 + (5.4031 - 5.70) * (t - 20) / 18.5  # ease to 5.40% at 38.5y


def build_boe_zip(rows_by_date: dict[date, dict[float, object]]) -> bytes:
    """A workbook laid out like the real one: title, blanks, 'years:', '#VALUE!', dated rows."""
    tenors = [i / 2 for i in range(1, 81)]
    wb = openpyxl.Workbook()
    wb.active.title = "info"
    for name in ("1. fwds, short end", "2. fwd curve", "3. spot, short end"):
        wb.create_sheet(name)
    ws = wb.create_sheet(BOE_SHEET)
    ws.append([None, "UK nominal spot curve"])
    ws.append([None])
    ws.append(["Maturity"])
    ws.append(["years:", *tenors])
    ws.append(["#VALUE!"])
    for d, values in rows_by_date.items():
        ws.append([datetime(d.year, d.month, d.day), *[values.get(t) for t in tenors]])
    buf = io.BytesIO()
    wb.save(buf)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(BOE_FILE, buf.getvalue())
        z.writestr("GLC Real daily data current month.xlsx", b"not used")
    return out.getvalue()


def full_day():
    return {i / 2: sample_rate(i / 2) for i in range(1, 81)}


# ---------------------------------------------------------------- parsing


def test_parse_latest_day_and_skip_blanks():
    day1 = full_day()
    day2 = full_day()
    day2[0.5] = ""  # the real file has '' and None in some cells
    day2[1.0] = None
    content = build_boe_zip({date(2026, 10, 28): day1, date(2026, 10, 29): day2})
    snap = parse_boe_zip(content, NOW)
    assert snap.observation_date == date(2026, 10, 29)
    assert snap.curve.currency == "GBP" and snap.curve.compounding == CONTINUOUS
    assert snap.rate_type == "zero" and snap.is_market_data
    tenors = [t for t, _ in snap.curve.points]
    assert tenors[0] == D("1.5") and tenors[-1] == D(40)  # blanks skipped, not invented
    assert len(tenors) == 78
    assert snap.curve.zero_rate(D(40)) == D("0.053273")


def test_latest_row_too_sparse_falls_back_to_previous_day():
    sparse = {0.5: 4.2, 1.0: 4.4}
    content = build_boe_zip({date(2026, 10, 28): full_day(), date(2026, 10, 29): sparse})
    assert parse_boe_zip(content, NOW).observation_date == date(2026, 10, 28)


def test_bad_inputs_raise_instead_of_inventing():
    with pytest.raises(MarketDataError):
        parse_boe_zip(b"not a zip", NOW)
    with pytest.raises(MarketDataError):
        parse_boe_zip(build_boe_zip({}), NOW)  # early in the month: no dated rows
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("something else.xlsx", b"x")
    with pytest.raises(MarketDataError):
        parse_boe_zip(empty.getvalue(), NOW)


# ---------------------------------------------------------------- compounding


def test_discount_factor_conventions():
    assert discount_factor(D("0.05"), D(2), CONTINUOUS) == (D("-0.1")).exp()
    assert abs(float(discount_factor(D("0.05"), D(2), CONTINUOUS)) - math.exp(-0.1)) < 1e-15
    assert discount_factor(D("0.05"), D(2), 2) == D("1.025") ** -4
    assert YieldCurve("GBP", ((D(1), D("0.05")), (D(2), D("0.05"))), compounding=CONTINUOUS).compounding_for(1) == 0
    assert YieldCurve("GBP", ((D(1), D("0.05")), (D(2), D("0.05")))).compounding_for(1) == 1


def test_continuous_curve_pricing_matches_quantlib():
    ql = pytest.importorskip("QuantLib")
    # Dates chosen so every payment date is a weekday (no business-day roll).
    bond = FixedRateBond("t", "t", "GBP", D(100), D("0.06"), 1, date(2026, 3, 30),
                         date(2036, 3, 31), ACT_ACT_ICMA, "GBP", D(0))
    curve = YieldCurve("GBP", ((D("0.5"), D("0.05")), (D(40), D("0.05"))), compounding=CONTINUOUS)
    settle = date(2026, 10, 30)
    mine = dirty_price_from_curve(bond, curve, settle)

    qd = lambda d: ql.Date(d.day, d.month, d.year)  # noqa: E731
    ql.Settings.instance().evaluationDate = qd(settle)
    cfs = [ql.SimpleCashFlow(float(p.coupon + p.principal), qd(p.pay_date))
           for p in bond.remaining_periods(settle)]
    ts = ql.FlatForward(qd(settle), 0.05, ql.Actual365Fixed(), ql.Continuous)
    ref = ql.CashFlows.npv(cfs, ql.YieldTermStructureHandle(ts), False, qd(settle), qd(settle))
    assert all(ql.Date.weekday(qd(p.pay_date)) not in (1, 7) for p in bond.remaining_periods(settle))
    assert abs(float(mine) - ref) < 1e-10


# ---------------------------------------------------------------- century bond


def test_century_bond_prices_off_gilt_curve():
    century = next(b for b in load_instruments(ROOT / "config" / "instruments.json") if b.currency == "GBP")
    snap = parse_boe_zip(build_boe_zip({date(2026, 10, 29): full_day()}), NOW)
    v = value_bond(century, snap.curve, date(2026, 10, 30))
    assert D(70) < v.fair_clean < D(110)
    assert D(10) < v.risk.modified_duration < D(18)
    # Payments after 40 years (the curve's end) are about 8% of value at ~6%
    # rates: small, but entirely dependent on the flat-extrapolation rule.
    assert D("0.04") < v.beyond_curve_share < D("0.12")
    up = next(s for s in v.shocks if s.name == "Benchmark +100 bp")
    assert up.change_pct < D(-8)


# ---------------------------------------------------------------- real downloads


REAL = sorted((ROOT / "tests" / "fixtures" / "real").glob("boe_*.zip"))


@pytest.mark.skipif(not REAL, reason="no real Bank of England downloads saved yet")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_real_boe_file(path):
    snap = parse_boe_zip(path.read_bytes(), NOW)
    assert snap.rate_type == "zero"
    assert snap.curve.compounding == CONTINUOUS
    assert len(snap.curve.points) >= 60
    assert snap.curve.longest_tenor == D(40)
    tenors = [t for t, _ in snap.curve.points]
    assert all(b - a == D("0.5") for a, b in zip(tenors, tenors[1:]))  # no gaps on the real file
    for _, r in snap.curve.points:
        assert D("-0.01") < r < D("0.15")
