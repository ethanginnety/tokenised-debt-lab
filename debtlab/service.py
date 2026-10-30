"""Glue: load curves and NAVs, value every instrument, record what happened."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone

from debtlab.audit.db import latest_curve, latest_nav, record_event, save_curve, save_nav
from debtlab.config import Settings
from debtlab.instruments import FundShare, NavReport
from debtlab.marketdata.adapters import (
    ONLINE_ADAPTERS,
    MarketDataError,
    illustrative_curves,
    to_zero_snapshot,
)
from debtlab.marketdata.models import MARKET_KINDS, CurveSnapshot, risk_increasing_allowed
from debtlab.money import q
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import price_yield_profile, value_bond


def load_curves(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None,
    failures: list[dict] | None = None,
) -> dict[str, CurveSnapshot]:
    """Offline: illustrative curves. Online: official sources, else last stored.

    Online mode never falls back to illustrative data. Each source that fails is
    appended to `failures` (if given), so the dashboard can say what went wrong.
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
        except Exception as exc:  # noqa: BLE001 - any failure (fetch, parse, bootstrap) falls back per currency
            previous = latest_curve(conn, ccy, MARKET_KINDS)
            record_event(conn, "marketdata_error",
                         {"error": str(exc), "error_type": type(exc).__name__,
                          "fallback": previous.id if previous else None},
                         entity=ccy)
            if previous:
                # Last valid observation, flagged by its age. Stored par curves
                # (from before the bootstrap existed) are converted on the way out.
                curves[ccy] = to_zero_snapshot(previous)
            if failures is not None:
                failures.append({"currency": ccy, "error_type": type(exc).__name__, "error": str(exc),
                                 "fallback_observation_date":
                                     previous.observation_date.isoformat() if previous else None})
    return curves


def with_current_gate(
    valuations: list[dict], curves: dict[str, CurveSnapshot], now: datetime, max_age_hours: int
) -> list[dict]:
    """Re-apply the quoting gate at read time, so a curve that ages after a refresh blocks quoting."""
    out = []
    for v in valuations:
        if v["status"] != "ok":
            out.append(v)  # never priced, so never quotable
            continue
        snap = curves.get(v["terms"]["benchmark"])
        allowed, reason = risk_increasing_allowed(snap, now, max_age_hours)
        out.append({**v, "trading_allowed": allowed, "trading_gate_reason": reason,
                    "curve_status": snap.status(now, max_age_hours) if snap else "missing"})
    return out


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
        if not b.issue_date <= settle < b.maturity_date:
            why = (f"not issued yet (issues {b.issue_date.isoformat()})" if settle < b.issue_date
                   else f"matured on {b.maturity_date.isoformat()}")
            record_event(conn, "valuation_skipped", {"reason": why, "settle": settle.isoformat()},
                         entity=b.id, idempotency_key=f"valuation_skipped:{b.id}:{settle.isoformat()}")
            out.append({"terms": terms, "status": "not_priced",
                        "message": f"Not priced for settlement on {settle.isoformat()}: {why}.",
                        "trading_allowed": False, "trading_gate_reason": why})
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
            # How the curve's rates compound (0 = continuous): effective duration is per unit of these.
            "curve_compounding": snap.curve.compounding_for(b.frequency),
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
            "long_end": [
                {
                    "name": c.name, "rule": c.rule,
                    "rate_at_maturity_pct": str(q(c.rate_at_maturity * 100, 4)),
                    "clean_price": str(q(c.clean_price)),
                    "change": str(q(c.change)),
                    "change_pct": str(q(c.change_pct, 4)),
                }
                for c in v.long_end
            ],
            "price_yield_profile": [
                {"ytm_pct": str(q(y * 100, 4)), "clean_price": str(q(p, 4))}
                for y, p in price_yield_profile(b, v.ytm, settle)
            ],
        }
        recorded = {k: result[k] for k in ("curve_snapshot", "fair_clean", "ytm_pct", "modified_duration", "settle")}
        # The key covers the results too: changed terms or pricing code on the same curve and date are recorded.
        digest = hashlib.sha256(json.dumps(recorded, sort_keys=True).encode("utf-8")).hexdigest()[:8]
        record_event(conn, "valuation", recorded, entity=b.id,
                     idempotency_key=f"valuation:{b.id}:{snap.id}:{settle.isoformat()}:{digest}")
        out.append(result)
    return out


def load_navs(conn: sqlite3.Connection, funds: list[FundShare]) -> dict[str, NavReport | None]:
    """Seed each fund's configured NAV report (once), then return the latest stored one."""
    out: dict[str, NavReport | None] = {}
    for f in funds:
        if save_nav(conn, f.initial_nav):
            record_event(conn, "nav_loaded", f.initial_nav.to_dict(), entity=f.id,
                         idempotency_key=f"nav_loaded:{f.initial_nav.id}")
        out[f.id] = latest_nav(conn, f.id)
    return out


NAV_GATE_REASON = "reported NAV is an appraisal, not an executable price; no secondary market until M5"


def fund_view(f: FundShare, nav: NavReport | None, now: datetime) -> dict:
    """What the dashboard shows for a fund share. Quoting is never allowed off a NAV."""
    terms = {
        "id": f.id, "name": f.name, "description": f.description, "currency": f.currency,
        "nav_frequency": f.nav_frequency, "nav_stale_after_days": f.nav_stale_after_days,
        "assumptions": list(f.assumptions),
    }
    base = {"terms": terms, "executable": False, "trading_allowed": False,
            "trading_gate_reason": NAV_GATE_REASON}
    if nav is None:
        return {**base, "nav_status": "missing", "message": "No NAV has been reported."}
    age = (now.date() - nav.as_of).days
    return {
        **base,
        "nav_status": "stale" if age > f.nav_stale_after_days else "current",
        "nav_per_share": str(nav.nav_per_share),
        "nav_as_of": nav.as_of.isoformat(),
        "nav_reported_at": nav.reported_at.isoformat(),
        "nav_source": nav.source,
        "nav_age_days": age,  # how old the valuation is
        "days_since_reported": (now.date() - nav.reported_at.date()).days,
    }
