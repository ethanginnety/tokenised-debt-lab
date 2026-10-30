"""The market maker's limits and state, stored in the database (M4 step 3).

Both are append-only: setting a limit or changing state adds a row, and the one
in force is the latest. Nothing lives only in memory, so a restart reads exactly
what was in force before it, and every change is audited with who made it and why.

State: *running* (may place orders), *paused* (may not; resume to run again) or
*killed*. The kill switch cancels every open order and can only be undone by an
explicit reset, with a reason, which leaves the bot paused rather than running.
A database where the bot has never been started reads as paused.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from debtlab.audit.db import atomic, record_event
from debtlab.ledger import is_cash
from debtlab.money import D, plain

LIMITS = {  # name -> what the scope is, and the unit
    "max_inventory": ("instrument", "face"),
    "max_order_size": ("instrument", "face"),
    "max_exposure_value": ("currency", "market value"),
    "max_exposure_dv01": ("currency", "per bp"),
    "max_daily_loss": ("currency", "currency"),
    "min_cash_reserve": ("currency", "currency"),
    "max_open_orders": ("all", "orders"),
    "max_data_age_hours": ("all", "hours"),
}
ALL = "all"
STATES = ("running", "paused", "killed")


class ControlError(ValueError):
    """A change the controls don't allow. Nothing is written."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- limits


def limit(conn: sqlite3.Connection, name: str, scope: str) -> Decimal:
    """The limit in force. Raises if it was never set: no limit means no trading, not no limit."""
    row = conn.execute("SELECT value FROM mm_limit_changes WHERE name = ? AND scope = ? ORDER BY id DESC LIMIT 1",
                       (name, scope)).fetchone()
    if row is None:
        raise ControlError(f"limit {name} for {scope} is not set")
    return D(row["value"])


def limits(conn: sqlite3.Connection) -> dict[tuple[str, str], Decimal]:
    """Every limit in force, {(name, scope): value}."""
    rows = conn.execute("SELECT name, scope, value FROM mm_limit_changes ORDER BY id")
    return {(r["name"], r["scope"]): D(r["value"]) for r in rows}  # later rows overwrite earlier


def set_limit(conn: sqlite3.Connection, name: str, scope: str, value: Decimal, by: str, reason: str,
              now: datetime | None = None) -> bool:
    """Record a new value. Returns False if it equals the value already in force."""
    if name not in LIMITS:
        raise ControlError(f"unknown limit {name}")
    kind = ALL if scope == ALL else "currency" if is_cash(scope) else "instrument"
    if kind != LIMITS[name][0]:
        raise ControlError(f"{name} is set per {LIMITS[name][0]}, not for {scope}")
    if value < 0:
        raise ControlError(f"{name} can't be negative")
    if not by.strip() or not reason.strip():
        raise ControlError("a limit change needs who and why")
    with atomic(conn):
        current = limits(conn).get((name, scope))
        if current == value:
            return False
        at = (now or _now()).isoformat()
        conn.execute("INSERT INTO mm_limit_changes(name, scope, value, changed_at, changed_by, reason)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (name, scope, plain(value), at, by, reason))
        record_event(conn, "limit_changed", {"name": name, "scope": scope, "value": plain(value),
                                             "previous": None if current is None else plain(current),
                                             "by": by, "reason": reason}, entity=f"{name}:{scope}")
    return True


def seed_limits(conn: sqlite3.Connection, path: Path) -> int:
    """Copy the starting limits from config, once each. A limit already in the database is left
    as it is, so a restart (or an edited config file) never loosens one changed since."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))["limits"]
    existing = limits(conn)
    added = 0
    with atomic(conn):
        for name in LIMITS:
            for scope, value in data[name].items():
                if (name, scope) not in existing:
                    added += set_limit(conn, name, scope, D(value), "config", "starting value from config")
    return added


# ---------------------------------------------------------------- state


def state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT state, changed_at, changed_by, reason FROM mm_state_changes"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"state": "paused", "changed_at": None, "changed_by": None, "reason": "never started"}
    return dict(row)


def _set_state(conn: sqlite3.Connection, new: str, by: str, reason: str, now: datetime | None) -> None:
    if not by.strip() or not reason.strip():
        raise ControlError("a state change needs who and why")
    old = state(conn)["state"]
    conn.execute("INSERT INTO mm_state_changes(state, changed_at, changed_by, reason) VALUES (?, ?, ?, ?)",
                 (new, (now or _now()).isoformat(), by, reason))
    record_event(conn, "mm_state_changed", {"from": old, "to": new, "by": by, "reason": reason})


def resume(conn: sqlite3.Connection, by: str, reason: str, now: datetime | None = None) -> None:
    with atomic(conn):
        current = state(conn)["state"]
        if current == "killed":
            raise ControlError("the kill switch is on: reset it (with a reason) before resuming")
        if current != "running":
            _set_state(conn, "running", by, reason, now)


def pause(conn: sqlite3.Connection, by: str, reason: str, now: datetime | None = None) -> int:
    """Stop placing orders and cancel the open ones. Returns how many were cancelled."""
    with atomic(conn):
        current = state(conn)["state"]
        cancelled = cancel_all(conn, f"paused: {reason}", now)
        if current == "running":
            _set_state(conn, "paused", by, reason, now)
    return cancelled


def kill(conn: sqlite3.Connection, by: str, reason: str, now: datetime | None = None) -> int:
    """The kill switch: cancel every open order and stop until an explicit reset."""
    with atomic(conn):
        cancelled = cancel_all(conn, f"kill switch: {reason}", now)
        if state(conn)["state"] != "killed":
            _set_state(conn, "killed", by, reason, now)
    return cancelled


def reset(conn: sqlite3.Connection, by: str, reason: str, now: datetime | None = None) -> None:
    """Turn the kill switch off. The bot is left paused: running again is a separate decision."""
    if not reason.strip():
        raise ControlError("resetting the kill switch needs a reason")
    with atomic(conn):
        if state(conn)["state"] != "killed":
            raise ControlError("the kill switch is not on")
        _set_state(conn, "paused", by, f"kill switch reset: {reason}", now)


def cancel_all(conn: sqlite3.Connection, reason: str, now: datetime | None = None) -> int:
    """Cancel every open order, each one audited. Returns how many."""
    at = (now or _now()).isoformat()
    with atomic(conn):
        ids = [r["id"] for r in conn.execute("SELECT id FROM mm_orders WHERE status = 'open' ORDER BY id")]
        for order_id in ids:
            conn.execute("UPDATE mm_orders SET status = 'cancelled', updated_at = ?, reason = ? WHERE id = ?",
                         (at, reason, order_id))
            record_event(conn, "order_cancelled", {"id": order_id, "reason": reason}, entity=order_id)
    return len(ids)
