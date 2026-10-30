"""Load simulated instruments from config/instruments.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from debtlab.money import D, bp
from debtlab.pricing.bond import FixedRateBond


@dataclass(frozen=True)
class NavReport:
    """A NAV the fund manager reported: what one share was worth on `as_of`.

    It is an appraisal, published with a lag, and never an executable price.
    """

    fund_id: str
    nav_per_share: Decimal
    as_of: date  # the date the NAV values the portfolio at
    reported_at: datetime  # when the manager published it (UTC)
    source: str

    def __post_init__(self) -> None:
        if self.nav_per_share <= 0:
            raise ValueError("NAV per share must be positive")
        if self.reported_at.tzinfo is None:
            raise ValueError("reported_at needs a timezone (UTC)")
        if self.reported_at.date() < self.as_of:
            raise ValueError("a NAV can't be reported before the date it values")

    @property
    def id(self) -> str:
        return f"{self.fund_id}:{self.as_of.isoformat()}:{self.reported_at.isoformat()}"

    def to_dict(self) -> dict:
        return {"fund_id": self.fund_id, "nav_per_share": str(self.nav_per_share),
                "as_of": self.as_of.isoformat(), "reported_at": self.reported_at.isoformat(),
                "source": self.source}

    @classmethod
    def from_dict(cls, fund_id: str, d: dict) -> "NavReport":
        return cls(fund_id=fund_id, nav_per_share=D(d["nav_per_share"]),
                   as_of=date.fromisoformat(d["as_of"]),
                   reported_at=datetime.fromisoformat(d["reported_at"]), source=d["source"])


@dataclass(frozen=True)
class FundShare:
    """A share in a simulated private-credit fund (instrument C).

    No coupons or maturity: its value is the latest reported NAV. Withdrawal
    mechanics (windows, caps, queue, gates) arrive in M5.
    """

    id: str
    name: str
    currency: str
    nav_frequency: str
    nav_stale_after_days: int
    initial_nav: NavReport  # seeded into storage once; later reports are appended there
    description: str = ""
    assumptions: tuple[str, ...] = field(default_factory=tuple)


Instrument = FixedRateBond | FundShare


def load_instruments(path: Path) -> list[Instrument]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Instrument] = []
    seen = set()
    for item in data["instruments"]:
        if item["id"] in seen:
            raise ValueError(f"duplicate instrument id {item['id']}")
        seen.add(item["id"])
        if item["type"] == "fixed_rate_bond":
            out.append(_bond(item))
        elif item["type"] == "fund_share":
            out.append(_fund(item))
        else:
            raise ValueError(f"unsupported instrument type {item['type']}")
    return out


def _bond(item: dict) -> FixedRateBond:
    return FixedRateBond(
        id=item["id"],
        name=item["name"],
        currency=item["currency"],
        face=D(item["face"]),
        coupon_rate=D(item["coupon_rate"]),
        frequency=int(item["frequency"]),
        issue_date=date.fromisoformat(item["issue_date"]),
        maturity_date=date.fromisoformat(item["maturity_date"]),
        day_count=item["day_count"],
        benchmark=item["benchmark"],
        credit_spread=bp(item["credit_spread_bp"]),
        description=item.get("description", ""),
        assumptions=tuple(item.get("assumptions", [])),
    )


def _fund(item: dict) -> FundShare:
    return FundShare(
        id=item["id"],
        name=item["name"],
        currency=item["currency"],
        nav_frequency=item["nav_frequency"],
        nav_stale_after_days=int(item["nav_stale_after_days"]),
        initial_nav=NavReport.from_dict(item["id"], item["reported_nav"]),
        description=item.get("description", ""),
        assumptions=tuple(item.get("assumptions", [])),
    )
