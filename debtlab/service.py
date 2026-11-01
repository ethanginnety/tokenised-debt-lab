"""Glue: load curves and NAVs, value every instrument, record what happened."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from debtlab import ledger, lifecycle
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
from debtlab.money import D, plain, q
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.dates import following
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


LIFECYCLE_OFF = ("Lifecycle runs only in a scenario database; this is the main database, which is never "
                 "advanced, so the market maker keeps a performing bond. To see it, create one with "
                 "python scripts/new_scenario_db.py and start the app with DEBTLAB_DB_PATH=data/scenario.sqlite3.")
PRICE_NOTE = ("Scenario prices are on the simulation date, using the curve this page loaded (today's), held "
              "constant: the scenario moves the calendar, not the market.")


def lifecycle_summary(conn: sqlite3.Connection, db_role: str | None, bonds: list[FixedRateBond]) -> dict:
    """The short block in /api/overview. Reads only."""
    if db_role != "scenario":
        return {"enabled": False, "reason": LIFECYCLE_OFF}
    sim = lifecycle.simulation_date(conn)
    return {
        "enabled": True,
        "simulation_date": sim.isoformat() if sim else None,
        "statuses": {b.id: lifecycle.instrument_status(conn, b.id)["status"] for b in bonds},
        "payments": conn.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0],
        "owed": len(lifecycle.unpaid(conn)),
    }


def lifecycle_view(
    conn: sqlite3.Connection, db_role: str | None, bonds: list[FixedRateBond],
    curves: dict[str, CurveSnapshot], valuations: list[dict],
) -> dict:
    """Everything /api/lifecycle shows: simulation date, each bond's state and price, payments,
    holders and amounts owed. Reads only: advancing and triggering stay in scripts/lifecycle.py."""
    if db_role != "scenario":
        return {"enabled": False, "reason": LIFECYCLE_OFF}
    sim = lifecycle.simulation_date(conn)
    today = {v["terms"]["id"]: v for v in valuations}
    out_bonds = []
    for b in bonds:
        st = lifecycle.instrument_status(conn, b.id)
        out_bonds.append({
            "id": b.id, "name": b.name, "currency": b.currency, **st,
            "units_outstanding": plain(-ledger.balance(conn, ledger.issuer_id(b.id), b.id)),
            "price": _scenario_price(conn, b, curves.get(b.benchmark), sim),
            "today": _today_price(today.get(b.id)),
            "default": _default_view(conn, b, sim),
        })
    balances = ledger.balances(conn)
    holders = [{
        "id": p["id"], "name": p["name"], "payments_blocked": p["payments_blocked"],
        "positions": {a: plain(v) for a, v in sorted(balances.get(p["id"], {}).items()) if not ledger.is_cash(a)},
        "cash": {a: plain(v) for a, v in sorted(balances.get(p["id"], {}).items()) if ledger.is_cash(a)},
    } for p in ledger.parties(conn) if p["role"] == "holder"]
    owed: dict[tuple[str, str], list[Decimal]] = {}
    for u in lifecycle.unpaid(conn):
        owed.setdefault((u["holder"], u["currency"]), []).append(D(u["amount_due"]))
    return {
        "enabled": True,
        "simulation_date": sim.isoformat() if sim else None,
        "price_note": PRICE_NOTE,
        "bonds": out_bonds,
        "payments": lifecycle.payments(conn),
        "holders": holders,
        "owed": [{"holder": h, "currency": c, "amount": plain(sum(v, Decimal(0))), "count": len(v)}
                 for (h, c), v in sorted(owed.items())],
        "owed_items": lifecycle.unpaid(conn),
        "integrity": ledger.check_integrity(conn),
    }


def _scenario_price(conn: sqlite3.Connection, b: FixedRateBond, snap: CurveSnapshot | None, on: date | None) -> dict:
    if on is None:
        return {"basis": "none", "quoting": "halted", "message": "no simulation date; recreate the scenario database"}
    if snap is None:
        return {"basis": "none", "quoting": "halted", "message": f"no {b.benchmark} curve loaded"}
    price = lifecycle.scenario_price(conn, b, snap.curve, on)
    return {**price, "curve": {"currency": b.benchmark, "kind": snap.kind, "snapshot_id": snap.id,
                               "source": snap.source, "observation_date": snap.observation_date.isoformat()}}


def _today_price(v: dict | None) -> dict | None:
    if v is None:
        return None
    if v["status"] != "ok":
        return {"status": v["status"], "message": v["message"]}
    return {"status": "ok", "settle": v["settle"], "clean": v["fair_clean"], "dirty": v["fair_dirty"]}


def _default_view(conn: sqlite3.Connection, b: FixedRateBond, sim: date | None) -> dict | None:
    """The default's fixed terms, its four steps (done, due but not yet run, or upcoming) and losses."""
    d = lifecycle.default_terms(conn, b.id)
    if d is None:
        return None
    declare_on, recovery_on = lifecycle.default_dates(d)
    claim = 100 + d["missed_per_100"]
    missed = conn.execute("SELECT status FROM payment_events WHERE id = ?", (d["missed_payment_id"],)).fetchone()
    rate_pct = plain(d["recovery_rate"] * 100)

    def step(name: str, on: date, done: bool, label: str) -> dict:
        state = "done" if done else "due" if sim is not None and on <= sim else "upcoming"
        return {"step": name, "date": on.isoformat(), "state": state, "label": label}

    return {
        "triggered_on": d["triggered_on"].isoformat(),
        "missed_payment_id": d["missed_payment_id"],
        "missed_pay_date": d["missed_pay_date"].isoformat(),
        "grace_days": d["grace_days"],
        "distressed_spread_bp": plain(d["distressed_spread_bp"]),
        "recovery_rate_pct": rate_pct,
        "recovery_delay_months": d["recovery_delay_months"],
        "claim_per_100": plain(claim),
        "recovery_per_100": plain(d["recovery_rate"] * claim),
        "timeline": [
            step("triggered", d["triggered_on"], True, "Default scenario triggered by hand"),
            step("missed", d["missed_pay_date"], missed is not None and missed["status"] == "missed",
                 f"Coupon missed; {d['grace_days']}-day grace period starts"),
            step("declared", declare_on, d["declared_on"] is not None,
                 f"Default declared after {d['grace_days']}-day grace"),
            step("recovered", following(recovery_on), d["recovery_paid_on"] is not None,
                 f"Recovery paid: {rate_pct}% of the claim, units retired"),
        ],
        "losses": lifecycle.default_losses(conn, b),
    }


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


# ---------------------------------------------------------------- market maker (M4 step 7)

MM_CONTROLS_NOTE = ("Read-only. Pause, resume, the kill switch, its reset, limit changes, runs and unwinds "
                    "are done with scripts/mm.py, each with who and why, and audited.")
MM_MARKS_NOTE = ("Marks are the bot's own fair values on the curves this page loaded. The day's P&L is worked "
                 "out from the ledger exactly as the daily-loss limit sees it; a simulation run's own curve moves "
                 "aren't stored, so after a run this differs from the run's total.")


def marketmaker_view(conn: sqlite3.Connection, bonds: list[FixedRateBond], curves: dict[str, CurveSnapshot],
                     now: datetime, max_curve_age_hours: int, runs: int = 10, quotes: int = 12) -> dict:
    """Everything /api/marketmaker shows. Reads only: nothing is quoted, opened or recorded."""
    from debtlab.marketmaker import controls, daily, simulation
    from debtlab.marketmaker.quoting import current_marks, recent_quotes

    mm = conn.execute("SELECT id, name FROM parties WHERE role = 'market_maker'").fetchone()
    if mm is None:
        return {"enabled": False, "label": simulation.LABEL,
                "reason": "No market maker in this database (a scenario database gets one only when created "
                          "with --market-maker)."}
    held = {a: v for a, v in ledger.balances(conn).get(mm["id"], {}).items() if v}
    marks = current_marks(conn, bonds, curves, now.date(), now, max_curve_age_hours)
    day = daily.daily_pnl(conn, marks, now, write=False)
    lim = controls.limits(conn)
    day_rows = []
    if day is not None:
        for ccy, v in sorted(day.items()):
            cap = lim.get(("max_daily_loss", ccy))
            used = None if v is None or cap in (None, 0) else plain(q(max(-v, Decimal(0)) / cap * 100, 1))
            day_rows.append({"currency": ccy, "pnl": None if v is None else plain(q(v, 2)),
                             "loss_limit": None if cap is None else plain(cap), "limit_used_pct": used})
    limit_rows = conn.execute(
        "SELECT name, scope, value, changed_at, changed_by, reason FROM mm_limit_changes WHERE id IN"
        " (SELECT MAX(id) FROM mm_limit_changes GROUP BY name, scope) ORDER BY name, scope").fetchall()
    run_rows = conn.execute("SELECT id, seed, status, started_at, last_step, params, summary FROM mm_runs"
                            " ORDER BY rowid DESC LIMIT ?", (runs,)).fetchall()
    return {
        "enabled": True,
        "label": simulation.LABEL,
        "controls_note": MM_CONTROLS_NOTE,
        "marks_note": MM_MARKS_NOTE,
        "party": {"id": mm["id"], "name": mm["name"]},
        "state": controls.state(conn),
        "holdings": {
            "units": {a: plain(v) for a, v in sorted(held.items()) if not ledger.is_cash(a)},
            "cash": {a: plain(v) for a, v in sorted(held.items()) if ledger.is_cash(a)},
        },
        "marks": {i: {"currency": m.currency, "dirty": plain(m.dirty), "dv01": plain(m.dv01),
                      "data_age_hours": plain(m.data_age_hours)} for i, m in sorted(marks.items())},
        "marks_on_market_data": all(c.is_market_data for c in curves.values()) if curves else False,
        "day": {"date": now.astimezone(timezone.utc).date().isoformat(), "opened": day is not None,
                "pnl": day_rows},
        "limits": [{**dict(r), "unit": controls.LIMITS.get(r["name"], ("", ""))[1]} for r in limit_rows],
        "open_orders": [dict(r) for r in conn.execute(
            "SELECT id, instrument_id, side, price, size, filled, created_at FROM mm_orders"
            " WHERE status = 'open' ORDER BY id")],
        "quotes": _with_run_steps(recent_quotes(conn, quotes), conn),
        "runs": [{"id": r["id"], "seed": r["seed"], "status": r["status"], "started_at": r["started_at"],
                  "steps_done": r["last_step"] + 1, "steps": json.loads(r["params"])["steps"],
                  "summary": json.loads(r["summary"]) if r["summary"] else None} for r in run_rows],
        "integrity": ledger.check_integrity(conn),
    }


def _with_run_steps(quotes: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """Tag each quote decision made inside a simulation run with that run and its step, since
    its time is on the run's simulated clock (which can run hours past the real time).
    A run quotes exactly at start + step x step length, so a decision belongs to the run
    whose start it sits a whole number of steps after; two runs started at the same instant
    can't be told apart, and the later one is named."""
    runs = []
    for r in conn.execute("SELECT id, started_at, params FROM mm_runs ORDER BY rowid DESC"):
        params = json.loads(r["params"])
        runs.append((r["id"], datetime.fromisoformat(r["started_at"]),
                     timedelta(seconds=int(params["step_seconds"])), int(params["steps"])))
    out = []
    for qd in quotes:
        at = datetime.fromisoformat(qd["quoted_at"])
        run = next(((rid, (at - start) // step) for rid, start, step, steps in runs
                    if start <= at <= start + steps * step and (at - start) % step == timedelta(0)), None)
        out.append({**qd, "run_id": run[0] if run else None, "step": run[1] if run else None})
    return out
