"""The live venue's database: its own SQLite file, migrated from migrations/xrpl_live/.

It records one network, testnet or mainnet, and refuses to open as the other: a new database
takes the network it's opened for; one already used keeps its own. Refuses a bond, monitor or
paper database. Limits and state are append-only and read from
the database every time, so a kill switch survives a restart. A database where the bot has
never run reads as paused. Never holds a key.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from debtlab.audit.db import connect, migrate  # noqa: F401  (connect re-exported)
from debtlab.money import D, plain
from debtlab.xrpl_market.paper.db import ControlError, WrongDatabase  # noqa: F401  (the same errors)
from debtlab.xrpl_market.store import _tx as tx, iso, parse  # noqa: F401  (re-exported)

MIGRATIONS = Path(__file__).resolve().parent.parent.parent.parent / "migrations" / "xrpl_live"
STATES = ("running", "paused", "killed")
LIMITS = {
    "max_order_eur": "EUR per order",
    "max_daily_loss_eur": "EUR per UTC day",
    "max_total_eur": "EUR: the account's value when opened, and its open orders together",
    "max_data_age_seconds": "seconds",
    "max_open_orders": "orders",
}


NETWORK_LABELS = {"testnet": "TESTNET: no real funds", "mainnet": "MAINNET: real funds"}


def open_db(path: Path | str, network: str = "testnet") -> sqlite3.Connection:
    if network not in NETWORK_LABELS:
        raise ValueError(f"unknown network {network!r}")
    conn = connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for marker, what in (({"ledger_txns", "curve_snapshots", "events"}, "a bond"), ({"xrpl_snapshots"}, "the monitor's"),
                         ({"paper_orders"}, "the paper bot's")):
        if tables & marker:
            conn.close()
            raise WrongDatabase(f"{path} is {what} database; the live venue uses its own")
    migrate(conn, MIGRATIONS)
    have = _network(conn)
    if have != network:
        used = any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                   for t in ("live_runs", "live_state_changes", "live_account", "live_txs"))
        if used or have != "testnet":      # only a new database's default mark can change, and never back from mainnet
            conn.close()
            raise WrongDatabase(f"{path} records {have}, not {network}: each network has its own database")
        with tx(conn):
            conn.execute("UPDATE live_meta SET value = ? WHERE key = 'network'", (network,))
    return conn


def _network(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT value FROM live_meta WHERE key = 'network'").fetchone()["value"]


def network(conn: sqlite3.Connection) -> str:
    """The network this database records: testnet or mainnet."""
    return _network(conn)


def label(conn: sqlite3.Connection) -> str:
    """What every event and message from this database says it is."""
    return NETWORK_LABELS[_network(conn)]


def event(conn: sqlite3.Connection, at: datetime, kind: str, detail: dict) -> None:
    conn.execute("INSERT INTO live_events(at, kind, detail) VALUES (?, ?, ?)",
                 (iso(at), kind, json.dumps(detail, default=str, sort_keys=True)))


def limits(conn: sqlite3.Connection) -> dict[str, Decimal]:
    return {r["name"]: D(r["value"]) for r in conn.execute("SELECT name, value FROM live_limit_changes ORDER BY id")}


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
        conn.execute("INSERT INTO live_limit_changes(name, value, changed_at, changed_by, reason) VALUES (?,?,?,?,?)",
                     (name, plain(value), iso(now), by, reason))
        event(conn, now, "limit_changed", {"name": name, "value": plain(value),
                                           "previous": None if current is None else plain(current),
                                           "by": by, "reason": reason})
    return True


def seed_limits(conn: sqlite3.Connection, cfg: dict, now: datetime) -> int:
    """The starting limits, from the same config as the paper bot; one already set is left alone."""
    have, added = limits(conn), 0
    with tx(conn):
        for name in LIMITS:
            if name not in have:
                added += set_limit(conn, name, D(cfg["paper"]["limits"][name]), "config", "starting value from config", now)
    return added


def state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT state, changed_at, changed_by, reason FROM live_state_changes"
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
    conn.execute("INSERT INTO live_state_changes(state, changed_at, changed_by, reason) VALUES (?,?,?,?)",
                 (new, iso(now), by, reason))
    event(conn, now, "state_changed", {"from": old, "to": new, "by": by, "reason": reason})


def start_run(conn: sqlite3.Connection, now: datetime, gap_seconds: float) -> tuple[int, float | None]:
    with tx(conn):
        hb = conn.execute("SELECT at FROM live_heartbeat WHERE id = 1").fetchone()
        gap = None
        if hb:
            last = parse(hb["at"])
            conn.execute("UPDATE live_runs SET stopped_at = ?, stop_reason = 'no clean stop (crash, sleep or power loss)'"
                         " WHERE stopped_at IS NULL", (iso(last),))
            gap = (now - last).total_seconds()
            if gap > gap_seconds:
                event(conn, now, "not_running", {"from": iso(last), "to": iso(now), "seconds": round(gap, 1)})
        run = conn.execute("INSERT INTO live_runs(started_at) VALUES (?)", (iso(now),)).lastrowid
        heartbeat(conn, run, now)
    return run, gap


def stop_run(conn: sqlite3.Connection, run_id: int, now: datetime, reason: str) -> None:
    with tx(conn):
        heartbeat(conn, run_id, now)
        conn.execute("UPDATE live_runs SET stopped_at = ?, stop_reason = ? WHERE id = ?", (iso(now), reason, run_id))


def heartbeat(conn: sqlite3.Connection, run_id: int, now: datetime) -> None:
    conn.execute("INSERT INTO live_heartbeat(id, at, run_id) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET at = excluded.at, run_id = excluded.run_id", (iso(now), run_id))
