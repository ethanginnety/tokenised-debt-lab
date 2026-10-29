"""Glue: load curves, value every instrument, record what happened."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from debtlab.audit.db import latest_curve, record_event, save_curve
from debtlab.config import Settings
from debtlab.marketdata.adapters import ONLINE_ADAPTERS, MarketDataError, illustrative_curves
from debtlab.marketdata.models import MARKET_KINDS, CurveSnapshot, risk_increasing_allowed
from debtlab.money import q
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import price_yield_profile, value_bond


def load_curves(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> dict[str, CurveSnapshot]:
    """Offline: illustrative curves. Online: official sources, else last stored.

    Online mode never falls back to illustrative data.
    """
    now = now or datetime.now(timezone.utc)
    curves: dict[str, CurveSnapshot] = {}
    if settings.data_mode == "offline":
        for ccy, snap in illustrative_curves(now).items():
            save_curve(conn, snap)
            curves[ccy] = snap
            record_event(conn, "curve_loaded", _curve_summary(snap), entity=ccy,
                         idempotency_key=f"curve_loaded:{snap.id}")
        return curves

    for ccy, fetch in ONLINE_ADAPTERS.items():
        try:
            snap = fetch(now)
            save_curve(conn, snap)
            curves[ccy] = snap
            record_event(conn, "curve_loaded", _curve_summary(snap), entity=ccy,
                         idempotency_key=f"curve_loaded:{snap.id}")
        except MarketDataError as exc:
            previous = latest_curve(conn, ccy, MARKET_KINDS)
            record_event(conn, "marketdata_error",
                         {"error": str(exc), "fallback": previous.id if previous else None},
                         entity=ccy)
            if previous:
                curves[ccy] = previous  # last valid observation, flagged by its age
    return curves


def _curve_summary(snap: CurveSnapshot) -> dict:
    return {
        "snapshot": snap.id,
        "kind": snap.kind,
        "rate_type": snap.rate_type,
        "observation_date": snap.observation_date.isoformat(),
        "points": len(snap.curve.points),
    }


def curve_view(snap: CurveSnapshot, now: datetime, max_age: int) -> dict:
    return {
        "currency": snap.curve.currency,
        "snapshot_id": snap.id,
        "source": snap.source,
        "source_url": snap.source_url,
        "kind": snap.kind,
        "rate_type": snap.rate_type,
        "observation_date": snap.observation_date.isoformat(),
        "fetched_at": snap.fetched_at.isoformat(),
        "age_hours": round(snap.age_hours(now), 1),
        "status": snap.status(now, max_age),
        "notes": snap.notes,
        "longest_tenor_years": str(snap.curve.longest_tenor),
        "points": [{"tenor_years": str(t), "rate_pct": str(q(r * 100, 4))} for t, r in snap.curve.points],
    }


def value_all(
    conn: sqlite3.Connection,
    bonds: list[FixedRateBond],
    curves: dict[str, CurveSnapshot],
    settle: date,
    settings: Settings,
    now: datetime | None = None,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for b in bonds:
        terms = {
            "id": b.id, "name": b.name, "description": b.description, "currency": b.currency,
            "face": str(b.face), "coupon_pct": str(q(b.coupon_rate * 100, 4)),
            "frequency": b.frequency, "issue_date": b.issue_date.isoformat(),
            "maturity_date": b.maturity_date.isoformat(), "day_count": b.day_count,
            "benchmark": b.benchmark, "credit_spread_bp": str(q(b.credit_spread * 10000, 2)),
            "assumptions": list(b.assumptions),
        }
        snap = curves.get(b.benchmark)
        allowed, reason = risk_increasing_allowed(snap, now, settings.max_curve_age_hours)
        if snap is None:
            record_event(conn, "valuation_skipped", {"reason": "no curve"}, entity=b.id)
            out.append({"terms": terms, "status": "no_data",
                        "message": f"No {b.benchmark} curve available, so the bond isn't priced.",
                        "trading_allowed": False, "trading_gate_reason": reason})
            continue
        v = value_bond(b, snap.curve, settle)
        result = {
            "terms": terms,
            "status": "ok",
            "curve_snapshot": snap.id,
            "curve_kind": snap.kind,
            "curve_status": snap.status(now, settings.max_curve_age_hours),
            "trading_allowed": allowed,
            "trading_gate_reason": reason,
            "settle": settle.isoformat(),
            "fair_clean": str(q(v.fair_clean)),
            "fair_dirty": str(q(v.fair_dirty)),
            "accrued": str(q(v.accrued)),
            "ytm_pct": str(q(v.ytm * 100, 4)),
            "benchmark_rate_at_maturity_pct": str(q(v.benchmark_rate_at_maturity * 100, 4)),
            "macaulay_duration": str(q(v.risk.macaulay_duration, 4)),
            "modified_duration": str(q(v.risk.modified_duration, 4)),
            "convexity": str(q(v.risk.convexity, 4)),
            "dv01": str(q(v.risk.dv01, 6)),
            "effective_duration": str(q(v.effective_duration, 4)),
            "effective_convexity": str(q(v.effective_convexity, 4)),
            "cash_flows_remaining": v.cash_flows_remaining,
            "years_to_maturity": str(q(v.years_to_maturity, 2)),
            "beyond_curve_share_pct": str(q(v.beyond_curve_share * 100, 2)),
            "shocks": [
                {
                    "name": s.name, "kind": s.kind, "shift_bp": s.shift_bp,
                    "clean_price": str(q(s.clean_price)),
                    "change_pct": str(q(s.change_pct, 4)),
                    "approx_change_pct": str(q(s.approx_change_pct, 4)),
                    "approx_error_pct": str(q(s.approx_error_pct, 4)),
                }
                for s in v.shocks
            ],
            "price_yield_profile": [
                {"ytm_pct": str(q(y * 100, 4)), "clean_price": str(q(p, 4))}
                for y, p in price_yield_profile(b, v.ytm, settle)
            ],
        }
        record_event(
            conn, "valuation",
            {k: result[k] for k in ("curve_snapshot", "fair_clean", "ytm_pct", "modified_duration", "settle")},
            entity=b.id,
            idempotency_key=f"valuation:{b.id}:{snap.id}:{settle.isoformat()}",
        )
        out.append(result)
    return out
