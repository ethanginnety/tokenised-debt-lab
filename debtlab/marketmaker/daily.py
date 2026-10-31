"""The day's P&L for the daily-loss limit (M4 step 6), worked out from the ledger, never passed in.

The day is the UTC calendar day of the clock (the simulated clock in a simulation).
The first time a day's P&L is needed, the market maker's position (cash and units, from
the ledger) and the marks in use are fixed as the day's open, in `mm_day_open`. After
that, per currency:

    day's P&L = change in cash + (units now x mark now - units at open x mark at open) / 100
                - what came in from outside since the open (funding and issue transactions)

at the bot's own marks (dirty, per 100). Coupons need no special case: the cash comes
in as the accrued interest in the dirty mark goes out. Fees and trades are P&L like
anything else the bot does.

A bond held at the open that couldn't be valued then is given its opening mark the
first time it is valued later that day, marked `late` and audited: moves before that
are missed, which is better than refusing bids in its currency all day. A bond held
now that can't be valued now makes its currency's P&L unknown (None), and the execution
check refuses bids in that currency, as for any exposure it can't value; if even its
currency isn't known, every currency is unknown.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from debtlab import ledger
from debtlab.audit.db import atomic, record_event
from debtlab.money import D, HUNDRED, ZERO, plain

if TYPE_CHECKING:
    from debtlab.marketmaker.execution import Mark

CAPITAL_KINDS = ("funding", "issue")  # money or units put in from outside: not P&L


def _market_maker(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT id FROM parties WHERE role = 'market_maker'").fetchone()["id"]


def day_open(conn: sqlite3.Connection, marks: dict[str, "Mark"], now: datetime, write: bool = True) -> dict | None:
    """The day's opening position and marks, fixed the first time they are asked for.
    With `write=False` (a status display) nothing is written: None if the day hasn't opened."""
    day = now.astimezone(timezone.utc).date().isoformat()
    row = conn.execute("SELECT * FROM mm_day_open WHERE day = ?", (day,)).fetchone()
    if row is None and not write:
        return None
    if row is None:
        mm = _market_maker(conn)
        held = ledger.balances(conn).get(mm, {})
        last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM ledger_entries").fetchone()[0]
        position = {asset: plain(v) for asset, v in sorted(held.items()) if v}
        opened = {i: {"currency": m.currency, "dirty": plain(m.dirty)} for i, m in sorted(marks.items())}
        with atomic(conn):
            conn.execute("INSERT INTO mm_day_open(day, opened_at, last_entry_id, position, marks) VALUES (?, ?, ?, ?, ?)",
                         (day, now.isoformat(), last, json.dumps(position), json.dumps(opened)))
            record_event(conn, "mm_day_opened", {"day": day, "position": position, "marks": opened},
                         idempotency_key=f"mm_day_opened:{day}")
        row = conn.execute("SELECT * FROM mm_day_open WHERE day = ?", (day,)).fetchone()
    opened = json.loads(row["marks"])
    missing = [a for a, v in json.loads(row["position"]).items()
               if not ledger.is_cash(a) and D(v) and a not in opened and a in marks]
    if missing and write:
        late = {a: {"currency": marks[a].currency, "dirty": plain(marks[a].dirty), "late": now.isoformat()}
                for a in missing}
        with atomic(conn):
            conn.execute("UPDATE mm_day_open SET marks = ? WHERE day = ?", (json.dumps({**opened, **late}), day))
            record_event(conn, "mm_day_open_marked_late", {"day": day, "marks": late},
                         idempotency_key=f"mm_day_open_marked_late:{day}:{','.join(sorted(missing))}")
        row = conn.execute("SELECT * FROM mm_day_open WHERE day = ?", (day,)).fetchone()
    return {"day": row["day"], "opened_at": row["opened_at"], "last_entry_id": row["last_entry_id"],
            "position": {a: D(v) for a, v in json.loads(row["position"]).items()},
            "marks": {i: (m["currency"], D(m["dirty"])) for i, m in json.loads(row["marks"]).items()}}


def _currency(conn: sqlite3.Connection, instrument_id: str) -> str | None:
    row = conn.execute("SELECT currency FROM instrument_currencies WHERE instrument_id = ?", (instrument_id,)).fetchone()
    return row["currency"] if row else None


def _capital_since(conn: sqlite3.Connection, mm: str, after_entry: int) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    rows = conn.execute(
        "SELECT e.asset, e.amount FROM ledger_entries e JOIN ledger_txns t ON t.id = e.txn_id"
        f" WHERE e.owner = ? AND e.id > ? AND t.kind IN ({','.join('?' for _ in CAPITAL_KINDS)})",
        (mm, after_entry, *CAPITAL_KINDS))
    for r in rows:
        out[r["asset"]] = out.get(r["asset"], ZERO) + D(r["amount"])
    return out


def daily_pnl(conn: sqlite3.Connection, marks: dict[str, "Mark"], now: datetime,
              write: bool = True) -> dict[str, Decimal | None] | None:
    """The day's P&L so far per currency, from the ledger; None where it can't be worked out.
    With `write=False`, None if the day hasn't opened yet (nothing is written)."""
    o = day_open(conn, marks, now, write)
    if o is None:
        return None
    mm = _market_maker(conn)
    held = ledger.balances(conn).get(mm, {})
    capital = _capital_since(conn, mm, o["last_entry_id"])
    opened = o["position"]
    assets = set(held) | set(opened) | set(capital)
    out: dict[str, Decimal | None] = {}
    for asset in sorted(a for a in assets if ledger.is_cash(a)):
        out[asset] = held.get(asset, ZERO) - opened.get(asset, ZERO) - capital.get(asset, ZERO)
    unknown: set[str | None] = set()
    for asset in sorted(a for a in assets if not ledger.is_cash(a)):
        units_now = held.get(asset, ZERO) - capital.get(asset, ZERO)  # units put in today aren't P&L
        units_open = opened.get(asset, ZERO)
        if not units_now and not units_open:
            continue
        ccy = marks[asset].currency if asset in marks else o["marks"].get(asset, (_currency(conn, asset), None))[0]
        if (units_now and asset not in marks) or (units_open and asset not in o["marks"]):
            unknown.add(ccy)
            continue
        value_now = units_now * marks[asset].dirty / HUNDRED if units_now else ZERO
        value_open = units_open * o["marks"][asset][1] / HUNDRED if units_open else ZERO
        out[ccy] = out.get(ccy, ZERO) + value_now - value_open
    if None in unknown:  # a holding whose currency isn't even known: every currency is in doubt
        return {c: None for c in out}
    for ccy in unknown:
        out[ccy] = None
    return out
