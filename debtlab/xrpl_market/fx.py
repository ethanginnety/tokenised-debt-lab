"""The ECB's EUR/USD reference rate (decision D): euro limits and depth are converted at
1 RLUSD = 1 USD (an assumption, risk R19) and this rate.

Daily, published around 16:00 CET on TARGET business days, so like the curves it is a
daily observation, not a live price. Stale after 96 hours, measured from midnight UTC on
its observation date, the same rule as the curves.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import httpx

from debtlab.money import D

ECB_EURUSD_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
                  "?lastNObservations=5&format=csvdata")
STALE_AFTER = timedelta(hours=96)
PAIR = "USD per EUR"


class FxError(Exception):
    pass


@dataclass(frozen=True)
class FxRate:
    obs_date: date
    usd_per_eur: Decimal
    source: str

    def age(self, now: datetime) -> timedelta:
        return now - datetime.combine(self.obs_date, time(0), tzinfo=timezone.utc)

    def stale(self, now: datetime) -> bool:
        return self.age(now) > STALE_AFTER


def parse_ecb_eurusd(text: str) -> FxRate:
    """The latest observation in the ECB's CSV. Raises rather than guesses."""
    rows = list(csv.DictReader(io.StringIO(text)))
    good = []
    for r in rows:
        if r.get("KEY") != "EXR.D.USD.EUR.SP00.A":
            raise FxError(f"unexpected series {r.get('KEY')!r}")
        v = (r.get("OBS_VALUE") or "").strip()
        if not v:
            continue
        rate = D(v)
        if not Decimal("0.5") < rate < Decimal("2.5"):
            raise FxError(f"implausible EUR/USD {rate} on {r.get('TIME_PERIOD')}")
        good.append((date.fromisoformat(r["TIME_PERIOD"]), rate))
    if not good:
        raise FxError("no EUR/USD observations in the ECB response")
    d, rate = max(good)
    return FxRate(d, rate, "ECB reference rate EXR.D.USD.EUR.SP00.A")


def fetch_ecb_eurusd(timeout: float = 20.0) -> tuple[FxRate, str]:
    try:
        r = httpx.get(ECB_EURUSD_URL, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise FxError(f"ECB EUR/USD: {e}") from e
    return parse_ecb_eurusd(r.text), r.text


def rlusd_to_eur(rlusd: Decimal, fx: FxRate) -> Decimal:
    """At 1 RLUSD = 1 USD (assumption, R19)."""
    return rlusd / fx.usd_per_eur


def eur_to_rlusd(eur: Decimal, fx: FxRate) -> Decimal:
    return eur * fx.usd_per_eur
