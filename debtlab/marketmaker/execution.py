"""The execution layer (M4 step 3): every order passes one check before it is placed.

The strategy proposes orders; this decides. The check reads the limits and the
bot's state from the database every time (never a copy in memory), and assumes
the worst: every open order on the same side fills. It lists every limit an order
would break, not just the first, and a refused order is audited with its reasons.
A daily-loss breach also pauses the bot, which cancels its open orders.

Daily P&L is passed in by the caller until the P&L split exists (M4 step 6).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from debtlab import ledger
from debtlab.audit.db import atomic, record_event
from debtlab.marketmaker import controls
from debtlab.money import HUNDRED, ZERO, money, plain

SIDES = ("buy", "sell")


@dataclass(frozen=True)
class Order:
    id: str  # deterministic: placing the same order again does nothing
    instrument_id: str
    side: str  # buy | sell
    price: Decimal  # clean, per 100 face
    size: Decimal  # face


@dataclass(frozen=True)
class Mark:
    """What the check needs to know about one instrument now."""
    currency: str
    dirty: Decimal  # fair value per 100, for market-value exposure
    accrued: Decimal  # per 100, as the ledger will charge it (zero when the bond trades flat)
    dv01: Decimal  # per 100 face, per bp
    data_age_hours: Decimal


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...] = ()  # every limit the order would break
    paused: bool = False  # a daily-loss breach paused the bot
    already_placed: bool = False


def market_maker_id(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT id FROM parties WHERE role = 'market_maker'").fetchall()
    if len(rows) != 1:
        raise controls.ControlError(f"expected one market maker in the ledger, found {len(rows)}")
    return rows[0]["id"]


def open_orders(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM mm_orders WHERE status = 'open' ORDER BY id")]


def check(conn: sqlite3.Connection, order: Order, marks: dict[str, Mark], daily_pnl: dict[str, Decimal],
          pending: tuple[Order, ...] = (), replacing: frozenset[str] = frozenset()) -> list[str]:
    """Every reason the order can't be placed now; empty if it can.

    `pending`: orders about to be placed with this one (the other side of a quote, other
    instruments), counted as open. `replacing`: open orders that will be cancelled first.
    """
    reasons: list[str] = []
    st = controls.state(conn)
    if st["state"] != "running":
        reasons.append(f"bot is {st['state']} ({st['reason']})")
    if order.side not in SIDES or order.size <= 0 or order.price <= 0:
        return reasons + [f"malformed order: {order.side} {order.size} at {order.price}"]
    mark = marks.get(order.instrument_id)
    if mark is None:
        return reasons + [f"no valuation for {order.instrument_id}"]
    lim = controls.limits(conn)

    def limit(name: str, scope: str) -> Decimal | None:
        if (name, scope) not in lim:
            reasons.append(f"limit {name} for {scope} is not set")
        return lim.get((name, scope))

    ccy, inst = mark.currency, order.instrument_id
    others = [o for o in open_orders(conn) if o["id"] != order.id and o["id"] not in replacing]
    others += [{"id": o.id, "instrument_id": o.instrument_id, "side": o.side, "price": o.price, "size": o.size}
               for o in pending if o.id != order.id]
    mm = market_maker_id(conn)

    if (cap := limit("max_order_size", inst)) is not None and order.size > cap:
        reasons.append(f"order size {order.size:,f} over the maximum {cap:,f}")
    if (cap := limit("max_open_orders", controls.ALL)) is not None and len(others) + 1 > cap:
        reasons.append(f"{len(others)} orders already open; the maximum is {cap}")
    if (cap := limit("max_data_age_hours", controls.ALL)) is not None and mark.data_age_hours > cap:
        reasons.append(f"data {mark.data_age_hours:.1f}h old; the maximum is {cap}h")
    if (cap := limit("max_daily_loss", ccy)) is not None and daily_pnl.get(ccy, ZERO) <= -cap:
        reasons.append(f"daily loss {-daily_pnl[ccy]:,f} {ccy} has reached the maximum {cap:,f}")

    def resting(instrument: str, side: str) -> Decimal:
        return sum((Decimal(o["size"]) for o in others if o["instrument_id"] == instrument and o["side"] == side), ZERO)

    held = ledger.balance(conn, mm, inst)
    if order.side == "buy":
        worst = held + resting(inst, "buy") + order.size
        if (cap := limit("max_inventory", inst)) is not None and worst > cap:
            reasons.append(f"inventory could reach {worst:,f} with open bids; the maximum is {cap:,f}")
    elif held - resting(inst, "sell") - order.size < 0:
        reasons.append(f"can't sell {order.size:,f}: {held:,f} held, {resting(inst, 'sell'):,f} already offered")

    # Exposure in this currency if every bid fills (offers would only reduce it).
    value = dv01 = ZERO
    for instrument, m in marks.items():
        if m.currency != ccy:
            continue
        units = ledger.balance(conn, mm, instrument) + resting(instrument, "buy")
        units += order.size if instrument == inst and order.side == "buy" else ZERO
        value += units * m.dirty / HUNDRED
        dv01 += units * m.dv01 / HUNDRED
    if (cap := limit("max_exposure_value", ccy)) is not None and value > cap:
        reasons.append(f"{ccy} market value could reach {money(value):,f}; the maximum is {cap:,f}")
    if (cap := limit("max_exposure_dv01", ccy)) is not None and dv01 > cap:
        reasons.append(f"{ccy} DV01 could reach {money(dv01):,f} per bp; the maximum is {cap:,f}")

    if order.side == "buy" and (floor := limit("min_cash_reserve", ccy)) is not None:
        def cost(o_size: Decimal, o_price: Decimal, m: Mark) -> Decimal:
            return o_size * (o_price + m.accrued) / HUNDRED
        committed = sum((cost(Decimal(o["size"]), Decimal(o["price"]), marks[o["instrument_id"]])
                         for o in others if o["side"] == "buy" and o["instrument_id"] in marks
                         and marks[o["instrument_id"]].currency == ccy), ZERO)
        left = ledger.balance(conn, mm, ccy) - committed - cost(order.size, order.price, mark)
        if left < floor:
            reasons.append(f"{ccy} cash would fall to {money(left):,f} if every bid fills; the reserve is {floor:,f}")
    return reasons


def place_order(conn: sqlite3.Connection, order: Order, marks: dict[str, Mark],
                daily_pnl: dict[str, Decimal], now: datetime | None = None) -> Decision:
    """Check, then record the order as open, or audit the refusal. All or nothing."""
    at = (now or datetime.now(timezone.utc)).isoformat()
    with atomic(conn):
        row = conn.execute("SELECT instrument_id, side, price, size FROM mm_orders WHERE id = ?", (order.id,)).fetchone()
        if row:
            if (row["instrument_id"], row["side"], Decimal(row["price"]), Decimal(row["size"])) != (
                    order.instrument_id, order.side, order.price, order.size):
                raise controls.ControlError(f"order {order.id} already placed with different terms")
            return Decision(True, already_placed=True)
        reasons = check(conn, order, marks, daily_pnl)
        payload = {"id": order.id, "instrument": order.instrument_id, "side": order.side,
                   "price": plain(order.price), "size": plain(order.size)}
        if reasons:
            breach = any(r.startswith("daily loss") for r in reasons)
            if breach and controls.state(conn)["state"] == "running":
                controls.pause(conn, "risk check", next(r for r in reasons if r.startswith("daily loss")), now)
            record_event(conn, "order_refused", {**payload, "reasons": reasons}, entity=order.instrument_id)
            return Decision(False, tuple(reasons), paused=breach)
        conn.execute("INSERT INTO mm_orders(id, instrument_id, side, price, size, status, created_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                     (order.id, order.instrument_id, order.side, plain(order.price), plain(order.size), at, at))
        record_event(conn, "order_placed", payload, entity=order.instrument_id,
                     idempotency_key=f"order_placed:{order.id}")
    return Decision(True)
