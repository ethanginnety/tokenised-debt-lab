"""Bond lifecycle in the scenario database: the simulation date and coupon payments.

A coupon is paid in two stages. Scheduling fixes who is owed what: the holders
at the end of the record date, each owed units x coupon per 100 / 100, rounded
to cents. Paying then posts one ledger transaction per holder, each in its own
all-or-nothing block that also marks that holder's entitlement paid. A run only
touches entitlements still due, so a run that stops halfway (a crash, a power
cut) is finished by running it again, and nobody is paid twice.

Everything here depends only on the ledger and the stored simulation date, so
the same database and date always give the same result.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from debtlab import ledger
from debtlab.audit.db import atomic, get_db_role, get_meta, record_event, set_meta
from debtlab.ledger import Line
from debtlab.money import D, ZERO, money
from debtlab.pricing.bond import FixedRateBond, Period

SIM_DATE_KEY = "simulation_date"


class LifecycleError(RuntimeError):
    """A lifecycle action that isn't allowed here. Nothing is written."""


def require_scenario(conn: sqlite3.Connection) -> None:
    """Lifecycle actions change history, so they run only in a scenario database."""
    role = get_db_role(conn)
    if role != "scenario":
        raise LifecycleError(
            f"lifecycle actions run only in a scenario database (this one is {role or 'unmarked'}); "
            "create one with scripts/new_scenario_db.py"
        )


# ---------------------------------------------------------------- simulation date


def simulation_date(conn: sqlite3.Connection) -> date | None:
    value = get_meta(conn, SIM_DATE_KEY)
    return date.fromisoformat(value) if value else None


def start_simulation(conn: sqlite3.Connection, start: date) -> None:
    """Set the first simulation date (once, when the scenario database is created)."""
    require_scenario(conn)
    if simulation_date(conn) is not None:
        raise LifecycleError("the simulation date is already set; advance it instead")
    set_meta(conn, SIM_DATE_KEY, start.isoformat())
    record_event(conn, "simulation_started", {"date": start.isoformat()},
                 idempotency_key="simulation_started")


def advance_simulation(conn: sqlite3.Connection, to: date) -> date:
    """Move the simulation date forward (never back). Returns the previous date."""
    require_scenario(conn)
    current = _current_date(conn)
    if to < current:
        raise LifecycleError(f"the simulation date only moves forward: it is {current}, not {to}")
    if to > current:
        with atomic(conn):
            set_meta(conn, SIM_DATE_KEY, to.isoformat())
            record_event(conn, "simulation_advanced", {"from": current.isoformat(), "to": to.isoformat()},
                         idempotency_key=f"simulation_advanced:{current.isoformat()}:{to.isoformat()}")
    return current


def _current_date(conn: sqlite3.Connection) -> date:
    current = simulation_date(conn)
    if current is None:
        raise LifecycleError("this scenario database has no simulation date; recreate it with "
                             "scripts/new_scenario_db.py")
    return current


# ---------------------------------------------------------------- coupons


@dataclass(frozen=True)
class RunResult:
    payment_id: str
    status: str
    total_due: Decimal
    total_paid: Decimal
    paid_now: int  # holders paid by this run
    unpaid: list[str]  # holders still owed, with the reason


def coupon_id(bond: FixedRateBond, period: Period) -> str:
    return f"coupon:{bond.id}:{period.pay_date.isoformat()}"


def due_coupons(bond: FixedRateBond, on: date) -> list[Period]:
    """Coupon periods whose payment date has arrived by `on` (the final one included)."""
    return [p for p in bond.periods() if p.pay_date <= on]


def schedule_coupon(conn: sqlite3.Connection, bond: FixedRateBond, period: Period) -> bool:
    """Fix the entitlements for one coupon from holdings at the end of its record date.

    Returns False if it was already scheduled. Afterwards the ledger refuses any
    movement in the bond dated on or before the record date (see ledger._books_open).
    """
    require_scenario(conn)
    pid = coupon_id(bond, period)
    if conn.execute("SELECT 1 FROM payment_events WHERE id = ?", (pid,)).fetchone():
        return False
    if period.pay_date > _current_date(conn):
        raise LifecycleError(f"{pid} isn't due until {period.pay_date}")
    record = bond.record_date(period.pay_date)
    holders = ledger.holdings(conn, bond.id, as_of=record)
    blocked = {p["id"] for p in ledger.parties(conn) if p["payments_blocked"]}
    exact = {h: units * period.coupon / bond.face for h, units in holders.items()}
    due = {h: money(v) for h, v in exact.items()}
    total_due = sum(due.values(), ZERO)
    residue = sum(exact.values(), ZERO) - total_due
    now = _now()
    with atomic(conn):
        conn.execute(
            "INSERT INTO payment_events(id, instrument_id, kind, pay_date, record_date, accrual_start,"
            " accrual_end, currency, amount_per_100, status, total_due, rounding_residue, created_at, updated_at)"
            " VALUES (?, ?, 'coupon', ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)",
            (pid, bond.id, period.pay_date.isoformat(), record.isoformat(), period.start.isoformat(),
             period.end.isoformat(), bond.currency, _text(period.coupon * 100 / bond.face),
             _text(total_due), _text(residue), now, now),
        )
        for h, units in holders.items():
            # A compliance hold is recorded now, at scheduling: the amount is owed but won't be paid.
            status, reason = ("unpaid", "payments blocked (compliance hold)") if h in blocked else ("due", "")
            conn.execute(
                "INSERT INTO entitlements(payment_id, holder, units, amount_due, status, reason)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (pid, h, _text(units), _text(due[h]), status, reason),
            )
        record_event(conn, "coupon_scheduled", {
            "id": pid, "record_date": record.isoformat(), "amount_per_100": _text(period.coupon * 100 / bond.face),
            "currency": bond.currency, "total_due": _text(total_due), "rounding_residue": _text(residue),
            "holders": {h: {"units": _text(u), "due": _text(due[h]), "blocked": h in blocked}
                        for h, u in holders.items()},
        }, entity=bond.id, idempotency_key=f"coupon_scheduled:{pid}")
    return True


def pay_coupon(conn: sqlite3.Connection, bond: FixedRateBond, period: Period,
               after_each: Callable[[str], None] | None = None) -> RunResult:
    """Schedule the coupon if needed, then pay every entitlement still due.

    Each holder is paid in its own atomic block: the ledger transaction and the
    entitlement's 'paid' mark are written together or not at all. `after_each`
    is called with the holder id once that block has committed (progress output,
    or a test that simulates a crash part-way through).
    """
    schedule_coupon(conn, bond, period)
    pid = coupon_id(bond, period)
    due = conn.execute("SELECT holder, amount_due FROM entitlements WHERE payment_id = ? AND status = 'due'"
                       " ORDER BY holder", (pid,)).fetchall()
    if due:
        _set_status(conn, pid, "in_progress")
    issuer = ledger.issuer_id(bond.id)
    for row in due:
        holder, amount = row["holder"], D(row["amount_due"])
        txn_id = f"{pid}:{holder}"
        with atomic(conn):
            if amount > 0:
                ledger.post(conn, txn_id, "coupon", period.pay_date,
                            [Line(issuer, bond.currency, -amount), Line(holder, bond.currency, amount)],
                            memo=f"{bond.id} coupon paid {period.pay_date.isoformat()}")
            conn.execute(
                "UPDATE entitlements SET status = 'paid', amount_paid = ?, txn_id = ?, paid_at = ?"
                " WHERE payment_id = ? AND holder = ? AND status = 'due'",
                (_text(amount), txn_id if amount > 0 else None, _now(), pid, holder),
            )
        if after_each:
            after_each(holder)
    return _finish(conn, pid, paid_now=len(due))


def pay_due_coupons(conn: sqlite3.Connection, bonds: list[FixedRateBond],
                    after_each: Callable[[str], None] | None = None) -> list[RunResult]:
    """Pay every coupon due by the simulation date, oldest first, for every bond."""
    require_scenario(conn)
    today = _current_date(conn)
    work = sorted(((p.pay_date, b.id, b, p) for b in bonds for p in due_coupons(b, today)),
                  key=lambda t: (t[0], t[1]))
    return [pay_coupon(conn, b, p, after_each) for _, _, b, p in work]


def _finish(conn: sqlite3.Connection, pid: str, paid_now: int) -> RunResult:
    rows = conn.execute("SELECT holder, amount_paid, status, reason FROM entitlements WHERE payment_id = ?"
                        " ORDER BY holder", (pid,)).fetchall()
    event = conn.execute("SELECT status, total_due, total_paid FROM payment_events WHERE id = ?", (pid,)).fetchone()
    total_due = D(event["total_due"])
    total_paid = sum((D(r["amount_paid"]) for r in rows), ZERO)
    unpaid = [f"{r['holder']} ({r['reason']})" for r in rows if r["status"] != "paid"]
    status = "partly_paid" if unpaid else "paid"
    if (event["status"], D(event["total_paid"])) == (status, total_paid):
        return RunResult(pid, status, total_due, total_paid, paid_now, unpaid)  # nothing changed
    with atomic(conn):
        conn.execute("UPDATE payment_events SET status = ?, total_paid = ?, updated_at = ? WHERE id = ?",
                     (status, _text(total_paid), _now(), pid))
        outcome = {"id": pid, "status": status, "total_due": _text(total_due),
                   "total_paid": _text(total_paid), "unpaid": unpaid}
        digest = hashlib.sha256(json.dumps(outcome, sort_keys=True).encode("utf-8")).hexdigest()[:8]
        record_event(conn, "coupon_run_finished", outcome, entity=pid.split(":")[1],
                     idempotency_key=f"coupon_run_finished:{pid}:{digest}")
    return RunResult(pid, status, total_due, total_paid, paid_now, unpaid)


def _set_status(conn: sqlite3.Connection, pid: str, status: str) -> None:
    conn.execute("UPDATE payment_events SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), pid))


# ---------------------------------------------------------------- reading


def payments(conn: sqlite3.Connection) -> list[dict]:
    """Every payment event with its entitlements, oldest first."""
    events = [dict(r) for r in conn.execute("SELECT * FROM payment_events ORDER BY pay_date, instrument_id")]
    for e in events:
        e["entitlements"] = [dict(r) for r in conn.execute(
            "SELECT holder, units, amount_due, amount_paid, status, reason, txn_id FROM entitlements"
            " WHERE payment_id = ? ORDER BY holder", (e["id"],))]
    return events


def _text(amount: Decimal) -> str:
    return f"{amount.normalize():f}"  # 90000, not 9E+4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
