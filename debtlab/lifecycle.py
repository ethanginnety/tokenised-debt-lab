"""Bond lifecycle in the scenario database: the simulation date, coupons, maturity and default.

A coupon is paid in two stages. Scheduling fixes who is owed what: the holders
at the end of the record date, each owed units x coupon per 100 / 100, rounded
to cents. Paying then posts one ledger transaction per holder, each in its own
all-or-nothing block that also marks that holder's entitlement paid. A run only
touches entitlements still due, so a run that stops halfway (a crash, a power
cut) is finished by running it again, and nobody is paid twice.

Principal at maturity works the same way, and each holder's transaction also
hands the units back to the issuer. Once the principal has been paid the bond's
status becomes 'matured'; anything a holder couldn't be paid stays recorded as owed.

The default scenario is triggered by hand: the bond's next coupon is missed,
a 30-day grace period runs, then default is declared, and 12 months later
holders recover 40% of their claim (face plus missed coupons), handing back
their units. In between, the bond is priced on a distressed basis and trades
flat (no accrued interest). See ASSUMPTIONS.md.

Everything here depends only on the ledger and the stored simulation date, so
the same database and date always give the same result.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from debtlab import ledger
from debtlab.audit.db import atomic, get_db_role, get_meta, record_event, set_meta
from debtlab.ledger import Line
from debtlab.money import D, ZERO, bp, money, q
from debtlab.pricing.bond import FixedRateBond, Period
from debtlab.pricing.curve import YieldCurve, discount_factor
from debtlab.pricing.dates import act_365f, add_months, following
from debtlab.pricing.engine import accrued_per_100, dirty_price_from_curve

SIM_DATE_KEY = "simulation_date"
REDEEMING = ("principal", "recovery")  # payments that hand units back to the issuer

# Default scenario terms, approved by Ethan on 30 Oct 2026. Stored with each default when triggered.
GRACE_DAYS = 30
DISTRESSED_SPREAD_BP = D(1000)
RECOVERY_RATE = D("0.40")
RECOVERY_DELAY_MONTHS = 12


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
    kind: str  # "coupon", "principal" or "recovery"
    scheduled_date: date  # the contractual date, before any weekend roll
    pay_date: date  # when cash moves: the scheduled date rolled to a business day
    accrual_start: date
    accrual_end: date
    amount_per_100: Decimal  # cash per 100 face

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.bond.id}:{self.pay_date.isoformat()}"

    @property
    def record_date(self) -> date:
        """Counted back from the scheduled date, as indentures do: 17 Feb 2029 (a Saturday,
        paid Monday 19 Feb) has its record date 15 days before the 17th, on 2 Feb."""
        return self.bond.record_date(self.scheduled_date)


def coupon_payment(bond: FixedRateBond, period: Period) -> Payment:
    return Payment(bond, "coupon", period.end, period.pay_date, period.start, period.end,
                   period.coupon * 100 / bond.face)


def principal_payment(bond: FixedRateBond) -> Payment:
    last = bond.periods()[-1]
    return Payment(bond, "principal", bond.maturity_date, last.pay_date, bond.issue_date, bond.maturity_date,
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
    if payment.kind in REDEEMING and holders != ledger.holdings(conn, bond.id):
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
        if payment.kind == "recovery":
            # The recovery is a share of the whole claim, missed coupons included, so the
            # separate missed-coupon amounts are settled by it, not still owed on top.
            conn.execute(
                "UPDATE entitlements SET status = 'written_off', reason = ? WHERE status = 'unpaid' AND payment_id = ?",
                (f"claim settled by the default recovery {pid}", default_terms(conn, bond.id)["missed_payment_id"]))
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
    d = default_terms(conn, payment.bond.id)
    if d and payment.kind != "recovery" and payment.pay_date >= d["missed_pay_date"]:
        raise LifecycleError(f"{payment.id} falls on or after {payment.bond.id}'s missed payment; "
                             "the issuer pays nothing more until the recovery")
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
        if payment.kind in REDEEMING:
            units = D(row["units"])
            lines += [Line(holder, bond.id, -units), Line(issuer, bond.id, units)]
        with atomic(conn):
            if lines:
                ledger.post(conn, txn_id, "redemption" if payment.kind in REDEEMING else "coupon",
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
    elif payment.kind == "recovery":
        _mark_recovered(conn, payment)
    return result


def pay_coupon(conn: sqlite3.Connection, bond: FixedRateBond, period: Period,
               after_each: Callable[[str], None] | None = None) -> RunResult:
    return pay(conn, coupon_payment(bond, period), after_each)


def pay_due(conn: sqlite3.Connection, bonds: list[FixedRateBond],
            after_each: Callable[[str], None] | None = None) -> list[RunResult]:
    """Do everything due by the simulation date, oldest first, for every bond.

    A performing bond pays its coupons, then principal. A bond whose default was
    triggered pays as normal until its missed coupon; that coupon is recorded as
    missed, nothing later is paid, default is declared when the grace period
    ends, and the recovery is paid on the recovery date.
    """
    require_scenario(conn)
    today = _current_date(conn)
    order = {"coupon": 0, "principal": 1, "declare": 2, "recovery": 3}  # same day: final coupon before principal
    work: list[tuple[date, str, int, Callable[[], RunResult | None]]] = []
    for b in bonds:
        d = default_terms(conn, b.id)
        for p in due_payments(b, today):
            if d is None or p.pay_date < d["missed_pay_date"]:
                work.append((p.pay_date, b.id, order[p.kind], lambda p=p: pay(conn, p, after_each)))
            elif p.kind == "coupon" and p.pay_date == d["missed_pay_date"]:
                work.append((p.pay_date, b.id, order["coupon"], lambda p=p: miss_payment(conn, p)))
            # Anything after the missed coupon is not paid: the claim is settled by the recovery.
        if d is not None:
            declare_on, recovery_on = default_dates(d)
            if declare_on <= today:
                work.append((declare_on, b.id, order["declare"], lambda b=b: declare_default(conn, b)))
            if following(recovery_on) <= today:  # built when it runs, after the declaration step
                work.append((following(recovery_on), b.id, order["recovery"],
                             lambda b=b: pay(conn, recovery_payment(conn, b), after_each)))
    work.sort(key=lambda w: w[:3])
    results = [step() for *_, step in work]
    return [r for r in results if r is not None]


def _finish(conn: sqlite3.Connection, pid: str, kind: str, paid_now: int) -> RunResult:
    rows = conn.execute("SELECT holder, amount_paid, status, reason FROM entitlements WHERE payment_id = ?"
                        " ORDER BY holder", (pid,)).fetchall()
    event = conn.execute("SELECT status, total_due, total_paid FROM payment_events WHERE id = ?", (pid,)).fetchone()
    total_due = D(event["total_due"])
    total_paid = sum((D(r["amount_paid"]) for r in rows), ZERO)
    unpaid_now = [f"{r['holder']} ({r['reason']})" for r in rows if r["status"] == "unpaid"]
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
        _set_instrument_status(conn, bond.id, "matured", principal.pay_date, reason)
        record_event(conn, "instrument_matured", {
            "instrument": bond.id, "since": principal.pay_date.isoformat(),
            "units_outstanding": _text(-ledger.balance(conn, ledger.issuer_id(bond.id), bond.id)),
            "unpaid": owed,
        }, entity=bond.id, idempotency_key=f"instrument_matured:{bond.id}")


def _set_instrument_status(conn: sqlite3.Connection, instrument_id: str, status: str, since: date,
                           reason: str) -> None:
    conn.execute(
        "INSERT INTO instrument_status(instrument_id, status, since, reason, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(instrument_id) DO UPDATE SET status = excluded.status, since = excluded.since,"
        " reason = excluded.reason, updated_at = excluded.updated_at",
        (instrument_id, status, since.isoformat(), reason, _now()))


# ---------------------------------------------------------------- default scenario


def default_terms(conn: sqlite3.Connection, instrument_id: str) -> dict | None:
    """The bond's triggered default, with its dates as `date`s and its numbers as Decimals, or None."""
    row = conn.execute("SELECT * FROM defaults WHERE instrument_id = ?", (instrument_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for k in ("triggered_on", "missed_pay_date", "missed_scheduled_date", "declared_on", "recovery_date"):
        d[k] = date.fromisoformat(d[k]) if d[k] else None
    for k in ("distressed_spread_bp", "recovery_rate", "missed_per_100"):
        d[k] = D(d[k])
    return d


def default_dates(d: dict) -> tuple[date, date]:
    """(declaration, recovery date before the weekend roll), both fixed by the terms at the trigger."""
    declared = d["missed_pay_date"] + timedelta(days=d["grace_days"])
    return declared, add_months(declared, d["recovery_delay_months"])


def trigger_default(conn: sqlite3.Connection, bond: FixedRateBond) -> Payment:
    """Start the default scenario: the bond's next coupon after the simulation date will be missed.

    Returns that coupon. Only a performing bond, once; nothing is paid or priced
    differently until the missed coupon's date arrives.
    """
    require_scenario(conn)
    today = _current_date(conn)
    status = instrument_status(conn, bond.id)["status"]
    if status != "performing" or default_terms(conn, bond.id) is not None:
        raise LifecycleError(f"{bond.id} is {status}" + (" and already set to default" if status == "performing" else "")
                             + "; only a performing bond can be defaulted")
    upcoming = [p for p in bond.periods() if p.pay_date > today]
    if not upcoming:
        raise LifecycleError(f"{bond.id} has no coupon left after {today} to miss")
    missed = coupon_payment(bond, upcoming[0])
    if conn.execute("SELECT 1 FROM payment_events WHERE id = ?", (missed.id,)).fetchone():
        raise LifecycleError(f"{missed.id} is already scheduled")  # can't happen while pay_date > today
    terms = {
        "triggered_on": today.isoformat(), "missed_payment_id": missed.id,
        "missed_pay_date": missed.pay_date.isoformat(), "missed_scheduled_date": missed.scheduled_date.isoformat(),
        "missed_per_100": _text(missed.amount_per_100), "grace_days": GRACE_DAYS,
        "distressed_spread_bp": _text(DISTRESSED_SPREAD_BP), "recovery_rate": _text(RECOVERY_RATE),
        "recovery_delay_months": RECOVERY_DELAY_MONTHS,
    }
    with atomic(conn):
        conn.execute(
            "INSERT INTO defaults(instrument_id, triggered_on, missed_payment_id, missed_pay_date,"
            " missed_scheduled_date, missed_per_100, grace_days, distressed_spread_bp, recovery_rate,"
            " recovery_delay_months, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bond.id, *terms.values(), _now()))
        record_event(conn, "default_triggered", {"instrument": bond.id, **terms}, entity=bond.id,
                     idempotency_key=f"default_triggered:{bond.id}")
    return missed


def miss_payment(conn: sqlite3.Connection, payment: Payment) -> RunResult:
    """The issuer doesn't pay: fix entitlements as usual, record every one unpaid, start the grace period."""
    bond, pid = payment.bond, payment.id
    schedule(conn, payment)
    if conn.execute("SELECT status FROM payment_events WHERE id = ?", (pid,)).fetchone()[0] != "missed":
        with atomic(conn):
            conn.execute("UPDATE entitlements SET status = 'unpaid', reason = 'issuer missed payment'"
                         " WHERE payment_id = ? AND status = 'due'", (pid,))
            _set_status(conn, pid, "missed")
            grace_end = payment.pay_date + timedelta(days=GRACE_DAYS)
            _set_instrument_status(conn, bond.id, "grace_period", payment.pay_date,
                                   f"missed {pid}; default declared on {grace_end} unless paid")
            record_event(conn, "payment_missed", {"id": pid, "grace_ends": grace_end.isoformat()},
                         entity=bond.id, idempotency_key=f"payment_missed:{pid}")
    rows = conn.execute("SELECT holder, reason FROM entitlements WHERE payment_id = ? ORDER BY holder",
                        (pid,)).fetchall()
    total_due = D(conn.execute("SELECT total_due FROM payment_events WHERE id = ?", (pid,)).fetchone()[0])
    return RunResult(pid, "missed", total_due, ZERO, 0, [f"{r['holder']} ({r['reason']})" for r in rows])


def declare_default(conn: sqlite3.Connection, bond: FixedRateBond) -> None:
    """End of grace with the coupon still unpaid: default declared, recovery date set. Once."""
    d = default_terms(conn, bond.id)
    if d is None or d["declared_on"] is not None:
        return
    declared, recovery = default_dates(d)
    claim = 100 + d["missed_per_100"]
    with atomic(conn):
        conn.execute("UPDATE defaults SET declared_on = ?, recovery_date = ? WHERE instrument_id = ?",
                     (declared.isoformat(), recovery.isoformat(), bond.id))
        _set_instrument_status(conn, bond.id, "defaulted", declared,
                               f"default declared after {d['grace_days']}-day grace; recovery expected {recovery}")
        record_event(conn, "default_declared", {
            "instrument": bond.id, "declared_on": declared.isoformat(), "recovery_date": recovery.isoformat(),
            "claim_per_100": _text(claim), "recovery_rate": _text(d["recovery_rate"]),
            "recovery_per_100": _text(d["recovery_rate"] * claim),
        }, entity=bond.id, idempotency_key=f"default_declared:{bond.id}")


def recovery_payment(conn: sqlite3.Connection, bond: FixedRateBond) -> Payment | None:
    """The recovery: rate x (face + missed coupons) per 100, on the recovery date. None before declaration."""
    d = default_terms(conn, bond.id)
    if d is None or d["recovery_date"] is None:
        return None
    per_100 = d["recovery_rate"] * (100 + d["missed_per_100"])
    return Payment(bond, "recovery", d["recovery_date"], following(d["recovery_date"]),
                   d["declared_on"], d["recovery_date"], per_100)


def _mark_recovered(conn: sqlite3.Connection, recovery: Payment) -> None:
    bond = recovery.bond
    if conn.execute("SELECT 1 FROM entitlements WHERE payment_id = ? AND status = 'due'",
                    (recovery.id,)).fetchone():
        return  # still being paid
    if conn.execute("SELECT recovery_paid_on FROM defaults WHERE instrument_id = ?", (bond.id,)).fetchone()[0]:
        return
    with atomic(conn):
        conn.execute("UPDATE defaults SET recovery_paid_on = ? WHERE instrument_id = ?",
                     (recovery.pay_date.isoformat(), bond.id))
        status = instrument_status(conn, bond.id)
        _set_instrument_status(conn, bond.id, "defaulted", date.fromisoformat(status["since"]),
                               f"recovery of {_text(recovery.amount_per_100)} per 100 paid {recovery.pay_date}")
        record_event(conn, "recovery_paid", {"instrument": bond.id, "losses": default_losses(conn, bond)},
                     entity=bond.id, idempotency_key=f"recovery_paid:{bond.id}")


def default_losses(conn: sqlite3.Connection, bond: FixedRateBond) -> list[dict]:
    """Per holder of record at the recovery: claim (face + missed coupons), recovered, owed, loss.

    The bond trades flat after the missed coupon, so the claim to it travels
    with the units: whoever holds them at the recovery's record date has it.
    Loss is claim minus what the recovery entitles the holder to; an entitlement
    not paid (Dana's hold) is shown as owed, not as a loss.
    """
    rec = recovery_payment(conn, bond)
    d = default_terms(conn, bond.id)
    if rec is None or d is None:
        return []
    rows = conn.execute("SELECT holder, units, amount_due, amount_paid, status FROM entitlements"
                        " WHERE payment_id = ? ORDER BY holder", (rec.id,)).fetchall()
    out = []
    for r in rows:
        claim = money(D(r["units"]) * (100 + d["missed_per_100"]) / 100)
        recovered = D(r["amount_due"])
        out.append({"holder": r["holder"], "units": r["units"], "claim": _text(claim),
                    "recovery_due": _text(recovered), "recovery_paid": _text(D(r["amount_paid"])),
                    "loss": _text(claim - recovered), "loss_pct": _text(q((claim - recovered) / claim * 100, 2)),
                    "status": r["status"]})
    return out


# ---------------------------------------------------------------- pricing on the simulation date


def scenario_price(conn: sqlite3.Connection, bond: FixedRateBond, curve: YieldCurve, on: date) -> dict:
    """Fair value per 100 on the simulation date, by the bond's lifecycle state.

    `curve` is today's curve held constant: times are measured from `on`.
    Performing: the usual curve + assumed spread, with accrued interest.
    From the missed coupon: trades flat (accrued 0, clean = dirty).
      Grace period: remaining payments at curve + distressed spread, plus the missed coupon at face (owed now).
      Defaulted: the recovery, rate x (face + missed coupons), discounted to its date at curve + distressed spread.
    """
    status = instrument_status(conn, bond.id)["status"]
    d = default_terms(conn, bond.id)
    base = {"status": status, "on": on.isoformat()}
    if status == "matured" or (d is not None and d.get("recovery_paid_on")):
        return {**base, "basis": "none", "quoting": "halted", "message": "nothing left to price: "
                + ("matured" if status == "matured" else "recovery paid")}
    if status == "performing":
        if not bond.issue_date <= on < bond.maturity_date:
            return {**base, "basis": "none", "quoting": "halted", "message": "outside the bond's life"}
        dirty = dirty_price_from_curve(bond, curve, on)
        accrued = accrued_per_100(bond, on)
        return {**base, "basis": "curve + assumed spread", "spread_bp": _text(bond.credit_spread * 10000),
                "dirty": _text(q(dirty)), "accrued": _text(q(accrued)), "clean": _text(q(dirty - accrued)),
                "quoting": "normal"}
    distressed = d["distressed_spread_bp"]
    comp = curve.compounding_for(bond.frequency)
    if status == "grace_period":
        # The engine adds a shift to the bond's own spread, so shift by the difference.
        later = (dirty_price_from_curve(bond, curve, on, distressed - bond.credit_spread * 10000)
                 if on < bond.maturity_date else ZERO)
        value = later + d["missed_per_100"]
        basis = "remaining payments at curve + distressed spread, plus the missed coupon at face"
        quoting = "restricted"
    else:  # defaulted, recovery not yet paid
        rec = recovery_payment(conn, bond)
        t = act_365f(on, rec.pay_date) if rec.pay_date > on else ZERO
        value = rec.amount_per_100 * discount_factor(curve.zero_rate(t) + bp(distressed), t, comp)
        basis = (f"recovery {_text(rec.amount_per_100)} per 100 "
                 f"({_text(d['recovery_rate'] * 100)}% of {_text(100 + d['missed_per_100'])}) "
                 f"due {rec.pay_date}, discounted at curve + distressed spread")
        quoting = "halted"
    return {**base, "basis": basis, "spread_bp": _text(distressed), "dirty": _text(q(value)),
            "accrued": "0", "clean": _text(q(value)), "flat": True, "quoting": quoting}


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
