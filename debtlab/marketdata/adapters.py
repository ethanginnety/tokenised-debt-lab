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
import zipfile
from datetime import date, datetime, timezone
from typing import Callable, TypeVar

T = TypeVar("T")

import httpx

from dataclasses import replace

from debtlab.marketdata.models import ILLUSTRATIVE, OFFICIAL_DAILY, CurveSnapshot
from debtlab.pricing.bootstrap import bootstrap_semiannual_par
from debtlab.money import D
from debtlab.pricing.curve import CONTINUOUS, YieldCurve, curve_from_percent, tenor_to_years


class MarketDataError(RuntimeError):
    """Raised instead of ever returning made-up numbers."""


def with_retries(fn: Callable[[], T], attempts: int = 3, base_delay: float = 1.0) -> T:
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
        notes="Raw par yields as published.",
    )


def to_zero_snapshot(snap: CurveSnapshot) -> CurveSnapshot:
    """Bootstrap a par snapshot into zero rates; other snapshots pass through."""
    if snap.rate_type != "par":
        return snap
    return replace(
        snap,
        curve=bootstrap_semiannual_par(snap.curve),
        rate_type="zero_bootstrapped",
        notes=(
            "Zero rates bootstrapped from the published par yields "
            "(semi-annual, par yields interpolated linearly)."
        ),
    )


def fetch_treasury(now: datetime | None = None) -> CurveSnapshot:
    now = now or datetime.now(timezone.utc)
    url = TREASURY_URL.format(year=now.year)
    text = with_retries(lambda: _get(url))
    return to_zero_snapshot(parse_treasury_csv(text, now, url))


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
    eur = curve_from_percent("EUR", points)
    return CurveSnapshot(
        curve=YieldCurve(eur.currency, eur.points, compounding=CONTINUOUS),
        source="ECB euro area AAA government spot curve",
        source_url=ECB_URL.format(tenor="{tenor}"),
        kind=OFFICIAL_DAILY,
        rate_type="zero",
        observation_date=date.fromisoformat(dates.pop()),
        fetched_at=fetched_at,
        notes="ECB Svensson spot rates, treated as continuously compounded (per the ECB data portal's series label; to confirm).",
    )


def fetch_ecb(now: datetime | None = None) -> CurveSnapshot:
    now = now or datetime.now(timezone.utc)
    texts = {t: with_retries(lambda t=t: _get(ECB_URL.format(tenor=t))) for t in ECB_TENORS}
    return parse_ecb_csv(texts, now)


# ------------------------------------------------------------ Bank of England


BOE_URL = (
    "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/"
    "latest-yield-curve-data.zip"
)
BOE_FILE = "GLC Nominal daily data current month.xlsx"
BOE_SHEET = "4. spot curve"


def parse_boe_zip(content: bytes, fetched_at: datetime, url: str = BOE_URL) -> CurveSnapshot:
    """Latest day of the BoE nominal gilt spot curve (0.5y to 40y, in percent).

    Layout, as downloaded 30 Oct 2026: in the zip, workbook BOE_FILE, sheet
    BOE_SHEET. One row starts with 'years:' and lists maturities; each data row
    starts with a date followed by spot rates. Blank cells (None or '') are
    maturities the Bank didn't estimate that day and are skipped, never filled.
    """
    try:
        import openpyxl  # imported here so the rest of the app runs without it
    except ImportError as exc:  # pragma: no cover
        raise MarketDataError("openpyxl is needed for the BoE curve: pip install openpyxl") from exc
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            data = z.read(BOE_FILE)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise MarketDataError(f"BoE zip missing {BOE_FILE!r}: {exc}") from exc
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = list(wb[BOE_SHEET].iter_rows(values_only=True))
    except KeyError as exc:
        raise MarketDataError(f"BoE workbook missing sheet {BOE_SHEET!r}") from exc

    header = next((r for r in rows if r and isinstance(r[0], str) and r[0].strip() == "years:"), None)
    if header is None:
        raise MarketDataError("BoE sheet has no 'years:' maturity row")
    dated = [r for r in rows if r and isinstance(r[0], datetime)]
    if not dated:
        raise MarketDataError("BoE sheet has no dated rows yet (early in the month?)")

    for row in reversed(dated):  # latest day that has a usable curve
        points = []
        for tenor, value in zip(header[1:], row[1:]):
            if isinstance(tenor, (int, float)) and isinstance(value, (int, float)):
                points.append((D(repr(float(tenor))), D(repr(float(value))) / D(100)))
        if len(points) >= 20:
            break
    else:
        raise MarketDataError("no BoE row has enough maturities")

    return CurveSnapshot(
        curve=YieldCurve("GBP", tuple(points), compounding=CONTINUOUS),
        source="Bank of England nominal gilt spot curve",
        source_url=url,
        kind=OFFICIAL_DAILY,
        rate_type="zero",
        observation_date=row[0].date(),
        fetched_at=fetched_at,
        notes=(
            f"Continuously compounded spot rates (BoE yield curve FAQ), "
            f"{points[0][0]}y to {points[-1][0]}y."
        ),
    )


def _get_bytes(url: str, timeout: float = 60.0) -> bytes:
    resp = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def fetch_boe(now: datetime | None = None) -> CurveSnapshot:
    now = now or datetime.now(timezone.utc)
    content = with_retries(lambda: _get_bytes(BOE_URL))
    return parse_boe_zip(content, now)


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
