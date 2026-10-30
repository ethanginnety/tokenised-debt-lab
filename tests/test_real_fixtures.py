"""Parse every real download saved by scripts/save_fixtures.py.

If no real files have been saved yet, these tests are skipped (not failed).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from debtlab.marketdata.adapters import parse_boe_zip, parse_ecb_csv, parse_treasury_csv, to_zero_snapshot
from debtlab.money import D
from debtlab.pricing.bootstrap import HALF, par_yield_from_zero
from debtlab.pricing.curve import CONTINUOUS

REAL = Path(__file__).parent / "fixtures" / "real"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
TREASURY = sorted(REAL.glob("treasury_*.csv"))
ECB = sorted(REAL.glob("ecb_*.json"))
BOE = sorted(REAL.glob("boe_*.zip"))


@pytest.mark.skipif(not TREASURY, reason="no real Treasury downloads saved yet")
@pytest.mark.parametrize("path", TREASURY, ids=lambda p: p.name)
def test_real_treasury_file(path):
    raw = parse_treasury_csv(path.read_text(encoding="utf-8"), NOW)
    assert raw.rate_type == "par"
    assert len(raw.curve.points) >= 10
    for _, r in raw.curve.points:
        assert D("-0.01") < r < D("0.20")  # sane levels, in fractions not percent
    zero = to_zero_snapshot(raw)
    for k in (1, 4, 20, 60):
        if HALF * k <= raw.curve.longest_tenor:
            assert abs(par_yield_from_zero(zero.curve, k) - raw.curve.zero_rate(HALF * k)) < D("1e-20")


@pytest.mark.skipif(not ECB, reason="no real ECB downloads saved yet")
@pytest.mark.parametrize("path", ECB, ids=lambda p: p.name)
def test_real_ecb_file(path):
    snap = parse_ecb_csv(json.loads(path.read_text(encoding="utf-8")), NOW)
    assert snap.rate_type == "zero"
    assert len(snap.curve.points) == 11
    for _, r in snap.curve.points:
        assert D("-0.02") < r < D("0.15")


@pytest.mark.skipif(not BOE, reason="no real Bank of England downloads saved yet")
@pytest.mark.parametrize("path", BOE, ids=lambda p: p.name)
def test_real_boe_file(path):
    snap = parse_boe_zip(path.read_bytes(), NOW)
    assert snap.rate_type == "zero"
    assert snap.curve.compounding == CONTINUOUS
    tenors = [t for t, _ in snap.curve.points]
    assert len(tenors) >= 20
    assert tenors[-1] <= D("40")
    assert all(b - a == HALF for a, b in zip(tenors, tenors[1:]))  # no gaps on the real file
    for _, r in snap.curve.points:
        assert D("-0.01") < r < D("0.15")
