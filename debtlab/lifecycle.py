"""Bond lifecycle in the scenario database: the simulation date, coupons and maturity.

A coupon is paid in two stages. Scheduling fixes who is owed what: the holders
at the end of the record date, each owed units x coupon per 100 / 100, rounded
to cents. Paying then posts one ledger transaction per holder, each in its own
all-or-nothing block that also marks that holder's entitlement paid. A run only
touches entitlements still due, so a run that stops halfway (a crash, a power
cut) is finished by running it again, and nobody is paid twice.

Principal at maturity works the same way, and each holder's transaction also
hands the units back to the issuer. Once the principal has been paid the bond's
status becomes 'matured'; anything a holder couldn't be paid stays recorded as owed.

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


@dataclass(frozen=True)
class Payment:
    """One scheduled payment on a bond: a coupon, or the principal at maturity."""

    bond: FixedRateBond
    kind: str  # "coupon" or "principal"
    pay_date: date
    accrual_start: date
    accrual_end: date
    amount_per_100: Decimal  # cash per 100 face

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.bond.id}:{self.pay_date.isoformat()}"

    @property
    def record_date(self) -> date:
        return self.bond.record_date(self.pay_date)


def coupon_payment(bond: FixedRateBond, period: Period) -> Payment:
    return Payment(bond, "coupon", period.pay_date, period.start, period.end, period.coupon * 100 / bond.face)


def principal_payment(bond: FixedRateBond) -> Payment:
    last = bond.periods()[-1]
    return Payment(bond, "principal", last.pay_date, bond.issue_date, bond.maturity_date,
                   last.principal * 100 / bond.face)


def coupon_id(bond: FixedRateBond, period: Period) -> str:
    return coupon_payment(bond, period).id


def due_payments(bond: FixedRateBond, on: date) -> list[Payment]:
    """Payments whose date has arrived by `on`: coupons (the final one included), then principal."""
    out = [coupon_payment(bond, p) for p in bond.periods() if p.pay_date <= on]
    principal = principal_payment(bond)
    return out + [principal] if principal.pay_date <= on else out


def schedule(conn: sqlite3.Connection, payment: Payment) -> bool:
    """Fix the entitlements for one payment from holdings at the end of its record date.

    Returns False if it was already scheduled. Afterwards the ledger refuses any
    movement in the bond dated on or before the record date (see ledger._books_open),
    and, once principal is scheduled, any movement in it but a redemption.
    """
    require_scenario(conn)
    bond, pid = payment.bond, payment.id
    if conn.execute("SELECT 1 FROM payment_events WHERE id = ?", (pid,)).fetchone():
        return False
    if payment.pay_date > _current_date(conn):
        raise LifecycleError(f"{pid} isn't due until {payment.pay_date}")
    record = payment.record_date
    holders = ledger.holdings(conn, bond.id, as_of=record)
    if payment.kind == "principal" and holders != ledger.holdings(conn, bond.id):
        # Units are retired from the holders of record, so they must still hold them.
        raise LifecycleError(f"{bond.id} units moved after its final record date {record}; "
                             "they can't be redeemed from the holders of record")
    blocked = {p["id"] for p in ledger.parties(conn) if p["payments_blocked"]}
    exact = {h: units * payment.amount_per_100 / 100 for h, units in holders.items()}
    due = {h: money(v) for h, v in exact.items()}
    total_due = sum(due.values(), ZERO)
    residue = sum(exact.values(), ZERO) - total_due
    now = _now()
    with atomic(conn):
        conn.execute(
            "INSERT INTO payment_events(id, instrument_id, kind, pay_date, record_date, accrual_start,"
            " accrual_end, currency, amount_per_100, status, total_due, rounding_residue, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)",
            (pid, bond.id, payment.kind, payment.pay_date.isoformat(), record.isoformat(),
             payment.accrual_start.isoformat(), payment.accrual_end.isoformat(), bond.currency,
             _text(payment.amount_per_100), _text(total_due), _text(residue), now, now),
        )
        for h, units in holders.items():
            # A compliance hold is recorded now, at scheduling: the amount is owed but won't be paid.
            status, reason = ("unpaid", "payments blocked (compliance hold)") if h in blocked else ("due", "")
            conn.execute(
                "INSERT INTO entitlements(payment_id, holder, units, amount_due, status, reason)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (pid, h, _text(units), _text(due[h]), status, reason),
            )
        record_event(conn, f"{payment.kind}_scheduled", {
            "id": pid, "record_date": record.isoformat(), "amount_per_100": _text(payment.amount_per_100),
            "currency": bond.currency, "total_due": _text(total_due), "rounding_residue": _text(residue),
            "holders": {h: {"units": _text(u), "due": _text(due[h]), "blocked": h in blocked}
                        for h, u in holders.items()},
        }, entity=bond.id, idempotency_key=f"{payment.kind}_scheduled:{pid}")
    return True


def schedule_coupon(conn: sqlite3.Connection, bond: FixedRateBond, period: Period) -> bool:
    return schedule(conn, coupon_payment(bond, period))


def pay(conn: sqlite3.Connection, payment: Payment,
        after_each: Callable[[str], None] | None = None) -> RunResult:
    """Schedule the payment if needed, then pay every entitlement still due.

    Each holder is paid in its own atomic block: the ledger transaction and the
    entitlement's 'paid' mark are written together or not at all. A principal
    payment also hands the holder's units back to the issuer in the same
    transaction, so units and cash move together. `after_each` is called with
    the holder id once that block has committed (progress output, or a test
    that simulates a crash part-way through).
    """
    schedule(conn, payment)
    bond, pid = payment.bond, payment.id
    due = conn.execute("SELECT holder, units, amount_due FROM entitlements WHERE payment_id = ? AND status = 'due'"
                       " ORDER BY holder", (pid,)).fetchall()
    if due:
        _set_status(conn, pid, "in_progress")
    issuer = ledger.issuer_id(bond.id)
    for row in due:
        holder, amount = row["holder"], D(row["amount_due"])
        txn_id = f"{pid}:{holder}"
        lines = [Line(issuer, bond.currency, -amount), Line(holder, bond.currency, amount)] if amount > 0 else []
        if payment.kind == "principal":
            units = D(row["units"])
            lines += [Line(holder, bond.id, -units), Line(issuer, bond.id, units)]
        with atomic(conn):
            if lines:
                ledger.post(conn, txn_id, "redemption" if payment.kind == "principal" else "coupon",
                            payment.pay_date, lines,
                            memo=f"{bond.id} {payment.kind} paid {payment.pay_date.isoformat()}")
            conn.execute(
                "UPDATE entitlements SET status = 'paid', amount_paid = ?, txn_id = ?, paid_at = ?"
                " WHERE payment_id = ? AND holder = ? AND status = 'due'",
                (_text(amount), txn_id if lines else None, _now(), pid, holder),
            )
        if after_each:
            after_each(holder)
    result = _finish(conn, pid, payment.kind, paid_now=len(due))
    if payment.kind == "principal":
        _mark_matured(conn, payment)
    return result


def pay_coupon(conn: sqlite3.Connection, bond: FixedRateBond, period: Period,
               after_each: Callable[[str], None] | None = None) -> RunResult:
    return pay(conn, coupon_payment(bond, period), after_each)


def pay_due(conn: sqlite3.Connection, bonds: list[FixedRateBond],
            after_each: Callable[[str], None] | None = None) -> list[RunResult]:
    """Make every payment due by the simulation date, oldest first: coupons, then principal."""
    require_scenario(conn)
    today = _current_date(conn)
    work = [p for b in bonds for p in due_payments(b, today)]
    work.sort(key=lambda p: (p.pay_date, p.bond.id, p.kind))  # on maturity, the final coupon before principal
    return [pay(conn, p, after_each) for p in work]


def _finish(conn: sqlite3.Connection, pid: str, kind: str, paid_now: int) -> RunResult:
    rows = conn.execute("SELECT holder, amount_paid, status, reason FROM entitlements WHERE payment_id = ?"
                        " ORDER BY holder", (pid,)).fetchall()
    event = conn.execute("SELECT status, total_due, total_paid FROM payment_events WHERE id = ?", (pid,)).fetchone()
    total_due = D(event["total_due"])
    total_paid = sum((D(r["amount_paid"]) for r in rows), ZERO)
    unpaid_now = [f"{r['holder']} ({r['reason']})" for r in rows if r["status"] != "paid"]
    status = "partly_paid" if unpaid_now else "paid"
    if (event["status"], D(event["total_paid"])) == (status, total_paid):
        return RunResult(pid, status, total_due, total_paid, paid_now, unpaid_now)  # nothing changed
    with atomic(conn):
        conn.execute("UPDATE payment_events SET status = ?, total_paid = ?, updated_at = ? WHERE id = ?",
                     (status, _text(total_paid), _now(), pid))
        outcome = {"id": pid, "status": status, "total_due": _text(total_due),
                   "total_paid": _text(total_paid), "unpaid": unpaid_now}
        digest = hashlib.sha256(json.dumps(outcome, sort_keys=True).encode("utf-8")).hexdigest()[:8]
        record_event(conn, f"{kind}_run_finished", outcome, entity=pid.split(":")[1],
                     idempotency_key=f"{kind}_run_finished:{pid}:{digest}")
    return RunResult(pid, status, total_due, total_paid, paid_now, unpaid_now)


def _set_status(conn: sqlite3.Connection, pid: str, status: str) -> None:
    conn.execute("UPDATE payment_events SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), pid))


# ---------------------------------------------------------------- instrument status


def instrument_status(conn: sqlite3.Connection, instrument_id: str) -> dict:
    """{'status', 'since', 'reason'}; an instrument with no row is performing."""
    row = conn.execute("SELECT status, since, reason FROM instrument_status WHERE instrument_id = ?",
                       (instrument_id,)).fetchone()
    return dict(row) if row else {"status": "performing", "since": None, "reason": ""}


def _mark_matured(conn: sqlite3.Connection, principal: Payment) -> None:
    """Matured once its principal payment has been made, set once. Anything unpaid stays recorded as owed."""
    bond = principal.bond
    if instrument_status(conn, bond.id)["status"] == "matured":
        return
    if conn.execute("SELECT 1 FROM entitlements WHERE payment_id = ? AND status = 'due'",
                    (principal.id,)).fetchone():
        return  # still being paid
    owed = unpaid(conn, bond.id)
    reason = "principal paid" + (f"; {len(owed)} amounts still owed to holders" if owed else "")
    with atomic(conn):
        conn.execute("INSERT INTO instrument_status(instrument_id, status, since, reason, updated_at)"
                     " VALUES (?, 'matured', ?, ?, ?)",
                     (bond.id, principal.pay_date.isoformat(), reason, _now()))
        record_event(conn, "instrument_matured", {
            "instrument": bond.id, "since": principal.pay_date.isoformat(),
            "units_outstanding": _text(-ledger.balance(conn, ledger.issuer_id(bond.id), bond.id)),
            "unpaid": owed,
        }, entity=bond.id, idempotency_key=f"instrument_matured:{bond.id}")


# ---------------------------------------------------------------- reading


def payments(conn: sqlite3.Connection) -> list[dict]:
    """Every payment event with its entitlements, oldest first."""
    events = [dict(r) for r in conn.execute("SELECT * FROM payment_events ORDER BY pay_date, instrument_id, kind")]
    for e in events:
        e["entitlements"] = [dict(r) for r in conn.execute(
            "SELECT holder, units, amount_due, amount_paid, status, reason, txn_id FROM entitlements"
            " WHERE payment_id = ? ORDER BY holder", (e["id"],))]
    return events


def unpaid(conn: sqlite3.Connection, instrument_id: str | None = None) -> list[dict]:
    """Every amount owed and not paid: payment, holder, currency, amount, reason."""
    sql = ("SELECT e.payment_id, p.instrument_id, p.kind, p.pay_date, p.currency, e.holder, e.amount_due, e.reason"
           " FROM entitlements e JOIN payment_events p ON p.id = e.payment_id WHERE e.status = 'unpaid'")
    args: tuple = ()
    if instrument_id:
        sql += " AND p.instrument_id = ?"
        args = (instrument_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY p.pay_date, p.instrument_id, p.kind, e.holder", args)]


def _text(amount: Decimal) -> str:
    return f"{amount.normalize():f}"  # 90000, not 9E+4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
