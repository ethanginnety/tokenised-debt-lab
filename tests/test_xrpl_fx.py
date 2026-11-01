"""ECB EUR/USD for the euro limits and depth (decision D)."""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from debtlab.xrpl_market import fx

REAL = sorted((Path(__file__).parent / "fixtures" / "real" / "xrpl").glob("ecb_eurusd_*.csv"))
HEAD = "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"


def row(d, v, key="EXR.D.USD.EUR.SP00.A"):
    return f"{key},D,USD,EUR,SP00,A,{d},{v}\n"


def test_latest_observation_wins_and_blanks_are_skipped():
    r = fx.parse_ecb_eurusd(HEAD + row("2026-10-29", "1.113") + row("2026-10-30", "1.1172") + row("2026-10-31", ""))
    assert (r.obs_date, r.usd_per_eur) == (date(2026, 10, 30), Decimal("1.1172"))


@pytest.mark.parametrize("text,why", [
    (HEAD, "no EUR/USD"),
    (HEAD + row("2026-10-30", "11.172"), "implausible"),
    (HEAD + row("2026-10-30", "1.1", key="EXR.D.GBP.EUR.SP00.A"), "unexpected series"),
])
def test_bad_responses_raise(text, why):
    with pytest.raises(fx.FxError, match=why):
        fx.parse_ecb_eurusd(text)


def test_stale_after_96_hours_from_midnight_of_the_observation_date():
    r = fx.FxRate(date(2026, 10, 30), Decimal("1.1172"), "ECB")
    assert not r.stale(datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc))
    assert r.stale(datetime(2026, 11, 3, 0, 1, tzinfo=timezone.utc))


def test_conversion_at_one_rlusd_one_dollar():
    r = fx.FxRate(date(2026, 10, 30), Decimal("1.25"), "ECB")
    assert fx.rlusd_to_eur(Decimal("12.5"), r) == 10 and fx.eur_to_rlusd(Decimal(10), r) == Decimal("12.5")


@pytest.mark.skipif(not REAL, reason="no real ECB file saved")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_real_ecb_file(path):
    r = fx.parse_ecb_eurusd(path.read_text(encoding="utf-8"))
    assert Decimal("0.8") < r.usd_per_eur < Decimal("1.6")
