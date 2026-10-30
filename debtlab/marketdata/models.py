"""Curve snapshots: a curve plus where it came from and how old it is."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal

from debtlab.money import D
from debtlab.pricing.curve import YieldCurve

OFFICIAL_DAILY = "official_daily"  # published once a day by a government/central bank
ILLUSTRATIVE = "illustrative"  # made-up shape for offline development; NOT market data

MARKET_KINDS = (OFFICIAL_DAILY,)


@dataclass(frozen=True)
class CurveSnapshot:
    curve: YieldCurve
    source: str
    source_url: str
    kind: str
    rate_type: str  # "par" (needs bootstrapping), "zero", or "illustrative"
    observation_date: date
    fetched_at: datetime  # UTC
    notes: str = ""

    @property
    def id(self) -> str:
        """Unique per source, date AND content: a re-processed curve gets a new id."""
        content = repr((self.rate_type, self.curve.compounding, self.curve.points))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
        return f"{self.curve.currency}:{self.source}:{self.observation_date.isoformat()}:{digest}"

    @property
    def is_market_data(self) -> bool:
        return self.kind in MARKET_KINDS

    def age_hours(self, now: datetime) -> float:
        observed = datetime.combine(self.observation_date, time(0), tzinfo=timezone.utc)
        return (now - observed).total_seconds() / 3600

    def status(self, now: datetime, max_age_hours: int) -> str:
        if not self.is_market_data:
            return "illustrative"
        return "stale" if self.age_hours(now) > max_age_hours else "fresh"

    def to_json(self) -> str:
        return json.dumps(
            {
                "currency": self.curve.currency,
                "points": [[str(t), str(r)] for t, r in self.curve.points],
                "compounding": self.curve.compounding,
                "source": self.source,
                "source_url": self.source_url,
                "kind": self.kind,
                "rate_type": self.rate_type,
                "observation_date": self.observation_date.isoformat(),
                "fetched_at": self.fetched_at.isoformat(),
                "notes": self.notes,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "CurveSnapshot":
        d = json.loads(text)
        points = tuple((D(t), D(r)) for t, r in d["points"])
        return cls(
            curve=YieldCurve(d["currency"], points, compounding=d.get("compounding")),
            source=d["source"],
            source_url=d["source_url"],
            kind=d["kind"],
            rate_type=d["rate_type"],
            observation_date=date.fromisoformat(d["observation_date"]),
            fetched_at=datetime.fromisoformat(d["fetched_at"]),
            notes=d.get("notes", ""),
        )


def risk_increasing_allowed(
    snapshot: CurveSnapshot | None, now: datetime, max_age_hours: int
) -> tuple[bool, str]:
    """Gate for any future action that adds risk (quoting, buying)."""
    if snapshot is None:
        return False, "no curve available"
    if not snapshot.is_market_data:
        return False, "illustrative data can't drive risk-taking"
    if snapshot.status(now, max_age_hours) == "stale":
        return False, f"curve is stale ({snapshot.age_hours(now):.0f}h old)"
    return True, "ok"


def rate_pct(value: Decimal) -> str:
    return f"{value * 100:.4f}"
