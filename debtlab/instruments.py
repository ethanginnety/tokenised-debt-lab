"""Load simulated instruments from config/instruments.json."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from debtlab.money import D, bp
from debtlab.pricing.bond import FixedRateBond


def load_instruments(path: Path) -> list[FixedRateBond]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    seen = set()
    for item in data["instruments"]:
        if item["type"] != "fixed_rate_bond":
            raise ValueError(f"unsupported instrument type {item['type']}")
        if item["id"] in seen:
            raise ValueError(f"duplicate instrument id {item['id']}")
        seen.add(item["id"])
        out.append(
            FixedRateBond(
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
        )
    return out
