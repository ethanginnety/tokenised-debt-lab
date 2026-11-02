"""The paper database: its own SQLite file, migrated from migrations/xrpl_paper/.

Refuses a bond database and the monitor's. Limits and the bot's state are append-only,
the latest row in force, read from the database every time (never kept in memory), so a
restart carries on with exactly what was in force: a kill switch survives a restart.
A database where the bot has never been started reads as paused.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from debtlab.audit.db import connect, migrate
from debtlab.money import D, plain
from debtlab.xrpl_market.store import _tx as tx, iso, parse  # noqa: F401  (re-exported for the paper modules)

MIGRATIONS = Path(__file__).resolve().parent.parent.parent.parent / "migrations" / "xrpl_paper"
STATES = ("running", "paused", "killed")
LIMITS = {  # name -> unit
    "max_order_eur": "EUR per order",
    "max_daily_loss_eur": "EUR per account per UTC day",
    "max_total_eur": "EUR: an account's starting value, and its open orders together",
    "max_data_age_seconds": "seconds",
    "max_open_orders": "orders per account",
}


class WrongDatabase(Exception):
    pass


class ControlError(ValueError):
    """A change the controls don't allow. Nothing is written."""


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables & {"ledger_txns", "curve_snapshots", "events"}:
        conn.close()
        raise WrongDatabase(f"{path} is a bond database; paper trading uses its own (e.g. data/xrpl_paper.sqlite3)")
    if "xrpl_snapshots" in tables:
        conn.close()
        raise WrongDatabase(f"{path} is the monitor's database; paper trading only reads it, and writes its own")
    migrate(conn, MIGRATIONS)
    return conn


def event(conn: sqlite3.Connection, at: datetime, kind: str, detail: dict) -> None:
    conn.execute("INSERT INTO paper_events(at, kind, detail) VALUES (?, ?, ?)",
                 (iso(at), kind, json.dumps(detail, default=str, sort_keys=True)))


# --- limits ----------------------------------------------------------------------------

def limits(conn: sqlite3.Connection) -> dict[str, Decimal]:
    return {r["name"]: D(r["value"]) for r in conn.execute("SELECT name, value FROM paper_limit_changes ORDER BY id")}


def set_limit(conn: sqlite3.Connection, name: str, value: Decimal, by: str, reason: str, now: datetime) -> bool:
    if name not in LIMITS:
        raise ControlError(f"unknown limit {name}; known: {', '.join(LIMITS)}")
    if value < 0:
        raise ControlError(f"{name} can't be negative")
    if not by.strip() or not reason.strip():
        raise ControlError("a limit change needs who and why")
    with tx(conn):
        current = limits(conn).get(name)
        if current == value:
            return False
        conn.execute("INSERT INTO paper_limit_changes(name, value, changed_at, changed_by, reason) VALUES (?,?,?,?,?)",
                     (name, plain(value), iso(now), by, reason))
        event(conn, now, "limit_changed", {"name": name, "value": plain(value),
                                           "previous": None if current is None else plain(current),
                                           "by": by, "reason": reason})
    return True


def seed_limits(conn: sqlite3.Connection, cfg: dict, now: datetime) -> int:
    """Copy the starting limits from config, once each; one already set is left alone."""
    have, added = limits(conn), 0
    with tx(conn):
        for name in LIMITS:
            if name not in have:
                added += set_limit(conn, name, D(cfg["paper"]["limits"][name]), "config", "starting value from config", now)
    return added


# --- state -----------------------------------------------------------------------------

def state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT state, changed_at, changed_by, reason FROM paper_state_changes"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"state": "paused", "changed_at": None, "changed_by": None, "reason": "never started"}
    return dict(row)


def set_state(conn: sqlite3.Connection, new: str, by: str, reason: str, now: datetime) -> None:
    if new not in STATES:
        raise ControlError(f"unknown state {new}")
    if not by.strip() or not reason.strip():
        raise ControlError("a state change needs who and why")
    old = state(conn)["state"]
    conn.execute("INSERT INTO paper_state_changes(state, changed_at, changed_by, reason) VALUES (?,?,?,?)",
                 (new, iso(now), by, reason))
    event(conn, now, "state_changed", {"from": old, "to": new, "by": by, "reason": reason})


# --- runs and heartbeat ----------------------------------------------------------------

def start_run(conn: sqlite3.Connection, now: datetime, gap_seconds: float) -> tuple[int, float | None]:
    """Start a run. Returns its id and, if the bot was last alive more than `gap_seconds`
    ago, how long it wasn't running (recorded as an event)."""
    with tx(conn):
        hb = conn.execute("SELECT at FROM paper_heartbeat WHERE id = 1").fetchone()
        gap = None
        if hb:
            last = parse(hb["at"])
            conn.execute("UPDATE paper_runs SET stopped_at = ?, stop_reason = 'no clean stop (crash, sleep or power loss)'"
                         " WHERE stopped_at IS NULL", (iso(last),))
            gap = (now - last).total_seconds()
            if gap > gap_seconds:
                event(conn, now, "not_running", {"from": iso(last), "to": iso(now), "seconds": round(gap, 1)})
        run = conn.execute("INSERT INTO paper_runs(started_at) VALUES (?)", (iso(now),)).lastrowid
        heartbeat(conn, run, now)
    return run, gap


def stop_run(conn: sqlite3.Connection, run_id: int, now: datetime, reason: str) -> None:
    with tx(conn):
        heartbeat(conn, run_id, now)
        conn.execute("UPDATE paper_runs SET stopped_at = ?, stop_reason = ? WHERE id = ?", (iso(now), reason, run_id))


def heartbeat(conn: sqlite3.Connection, run_id: int, now: datetime) -> None:
    conn.execute("INSERT INTO paper_heartbeat(id, at, run_id) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET at = excluded.at, run_id = excluded.run_id", (iso(now), run_id))
