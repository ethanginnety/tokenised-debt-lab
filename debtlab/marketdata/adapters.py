"""Where curves come from.

Official sources publish once per business day. Nothing here is a live tick.
Parsing is separated from fetching so it can be tested without a network.

IMPORTANT: the parsers follow each source's documented format, but this code
was written in an environment that could not reach the sources. Verify the
first real download (see RUNBOOK.md, "First online run").
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime, timezone
from typing import Callable

import httpx

from debtlab.marketdata.models import ILLUSTRATIVE, OFFICIAL_DAILY, CurveSnapshot
from debtlab.pricing.curve import curve_from_percent, tenor_to_years


class MarketDataError(RuntimeError):
    """Raised instead of ever returning made-up numbers."""


def with_retries(fn: Callable[[], str], attempts: int = 3, base_delay: float = 1.0) -> str:
    """Call `fn`, retrying with exponential backoff (1s, 2s, 4s ...)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below as MarketDataError
            last = exc
            if i < attempts - 1:
                time.sleep(base_delay * (2**i))
    raise MarketDataError(f"fetch failed after {attempts} attempts: {last}")


# Some official sites (the US Treasury especially) answer requests that don't
# look like a browser very slowly: measured on 30 Oct 2026 at ~17s vs 0.4s.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _get(url: str, timeout: float = 30.0) -> str:
    resp = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


# ------------------------------------------------------------ US Treasury


TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)


def parse_treasury_csv(text: str, fetched_at: datetime, url: str = "") -> CurveSnapshot:
    """Latest row of the Treasury daily par yield curve CSV (rates in percent)."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise MarketDataError("Treasury CSV had no rows")
    dated = []
    for row in rows:
        try:
            dated.append((datetime.strptime(row["Date"].strip(), "%m/%d/%Y").date(), row))
        except (KeyError, ValueError) as exc:
            raise MarketDataError(f"unexpected Treasury date format: {exc}") from exc
    obs_date, latest = max(dated, key=lambda x: x[0])
    points = {}
    for col, val in latest.items():
        if col == "Date" or val is None or not val.strip():
            continue  # a blank cell is skipped, never filled in
        try:
            tenor_to_years(col)
        except ValueError:
            continue
        points[col] = val.strip()
    if len(points) < 4:
        raise MarketDataError("Treasury row had too few usable tenors")
    return CurveSnapshot(
        curve=curve_from_percent("USD", points),
        source="US Treasury daily par yield curve",
        source_url=url or TREASURY_URL,
        kind=OFFICIAL_DAILY,
        rate_type="par",
        observation_date=obs_date,
        fetched_at=fetched_at,
        notes="Par yields used directly as discount rates until the bootstrap lands (M2).",
    )


def fetch_treasury(now: datetime | None = None) -> CurveSnapshot:
    now = now or datetime.now(timezone.utc)
    url = TREASURY_URL.format(year=now.year)
    text = with_retries(lambda: _get(url))
    return parse_treasury_csv(text, now, url)


# ------------------------------------------------------------ ECB


ECB_TENORS = ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y")
ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/YC/"
    "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_{tenor}?lastNObservations=1&format=csvdata"
)


def parse_ecb_csv(by_tenor: dict[str, str], fetched_at: datetime) -> CurveSnapshot:
    """One csvdata response per tenor from the ECB data API (rates in percent)."""
    points: dict[str, str] = {}
    dates = set()
    for tenor, text in by_tenor.items():
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or "OBS_VALUE" not in rows[-1]:
            raise MarketDataError(f"ECB response for {tenor} had no OBS_VALUE")
        points[tenor] = rows[-1]["OBS_VALUE"]
        dates.add(rows[-1]["TIME_PERIOD"])
    if len(dates) != 1:
        raise MarketDataError(f"ECB tenors came from different dates: {sorted(dates)}")
    return CurveSnapshot(
        curve=curve_from_percent("EUR", points),
        source="ECB euro area AAA government spot curve",
        source_url=ECB_URL.format(tenor="{tenor}"),
        kind=OFFICIAL_DAILY,
        rate_type="zero",
        observation_date=date.fromisoformat(dates.pop()),
        fetched_at=fetched_at,
        notes="ECB spot rates; compounding convention to be confirmed in M2.",
    )


def fetch_ecb(now: datetime | None = None) -> CurveSnapshot:
    now = now or datetime.now(timezone.utc)
    texts = {t: with_retries(lambda t=t: _get(ECB_URL.format(tenor=t))) for t in ECB_TENORS}
    return parse_ecb_csv(texts, now)


# ------------------------------------------------------------ Bank of England


def fetch_boe(now: datetime | None = None) -> CurveSnapshot:
    """Gilt curves are published as spreadsheets; adapter scheduled for M2."""
    raise MarketDataError("GBP gilt curve adapter not built yet (milestone M2)")


ONLINE_ADAPTERS: dict[str, Callable[[datetime], CurveSnapshot]] = {
    "USD": fetch_treasury,
    "EUR": fetch_ecb,
    "GBP": fetch_boe,
}


# ------------------------------------------------------------ Illustrative


def illustrative_curves(now: datetime) -> dict[str, CurveSnapshot]:
    """Hand-set curves for OFFLINE development only. Never market data."""
    gbp = curve_from_percent(
        "GBP",
        {"1Y": 3.90, "2Y": 4.00, "5Y": 4.60, "10Y": 5.40, "20Y": 5.90, "30Y": 6.00, "50Y": 5.70},
    )
    usd = curve_from_percent(
        "USD",
        {"3M": 3.95, "1Y": 3.70, "2Y": 3.60, "3Y": 3.65, "5Y": 3.80, "10Y": 4.20, "30Y": 4.70},
    )
    today = now.date()
    return {
        "GBP": CurveSnapshot(
            gbp, "Illustrative GBP curve", "", ILLUSTRATIVE, "illustrative", today, now,
            "Not market data. 10Y and 30Y set near levels reported in the press on "
            "14 Oct 2026 (10Y ~5.4%, 30Y ~6%); other points chosen for shape.",
        ),
        "USD": CurveSnapshot(
            usd, "Illustrative USD curve", "", ILLUSTRATIVE, "illustrative", today, now,
            "Not market data. All points chosen for a plausible shape.",
        ),
    }
