"""The execution layer (M4 step 3): every order passes one check before it is placed.

The strategy proposes orders; this decides. The check reads the limits and the
bot's state from the database every time (never a copy in memory), and assumes
the worst: every open order on the same side fills. It lists every limit an order
would break, not just the first, and a refused order is audited with its reasons.
A daily-loss breach also pauses the bot, which cancels its open orders.

The day's P&L is worked out here from the ledger (`daily.daily_pnl`), never passed in
by the caller (M4 step 6): what the bot is told can't hide a loss from the limit.

`LedgerRiskCheck` is the default `RiskCheck`; `place_order` sends an order that passes it
to the active venue, and `book_fill` books a fill from any venue (see `interfaces`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from debtlab import ledger
from debtlab.audit.db import atomic, record_event
from debtlab.marketmaker import controls, daily
from debtlab.marketmaker.interfaces import Order, RiskCheck, active_venue  # noqa: F401  (Order lives there now)
from debtlab.money import HUNDRED, ZERO, money, plain

SIDES = ("buy", "sell")


@dataclass(frozen=True)
class Mark:
    """What the bond check needs to know about one instrument now (an `interfaces.Mark`)."""
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
    """Open orders, each with `remaining` = size less what has filled."""
    rows = conn.execute("SELECT * FROM mm_orders WHERE status = 'open' ORDER BY id")
    return [{**dict(r), "remaining": Decimal(r["size"]) - Decimal(r["filled"])} for r in rows]


def check(conn: sqlite3.Connection, order: Order, marks: dict[str, Mark], now: datetime | None = None,
          pending: tuple[Order, ...] = (), replacing: frozenset[str] = frozenset()) -> list[str]:
    """Every reason the order can't be placed now; empty if it can.

    `now` sets the day for the daily-loss limit. `pending`: orders about to be placed with this one (the other side of a quote, other
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
    others += [{"id": o.id, "instrument_id": o.instrument_id, "side": o.side, "price": o.price, "remaining": o.size}
               for o in pending if o.id != order.id]
    mm = market_maker_id(conn)

    if (cap := limit("max_order_size", inst)) is not None and order.size > cap:
        reasons.append(f"order size {order.size:,f} over the maximum {cap:,f}")
    if (cap := limit("max_open_orders", controls.ALL)) is not None and len(others) + 1 > cap:
        reasons.append(f"{len(others)} orders already open; the maximum is {cap}")
    if (cap := limit("max_data_age_hours", controls.ALL)) is not None and mark.data_age_hours > cap:
        reasons.append(f"data {mark.data_age_hours:.1f}h old; the maximum is {cap}h")
    day = daily.daily_pnl(conn, marks, now or datetime.now(timezone.utc))
    if day.get(ccy, ZERO) is None:
        if order.side == "buy":
            reasons.append(f"the day's {ccy} P&L is unknown: a holding can't be valued")
    elif (cap := limit("max_daily_loss", ccy)) is not None and day.get(ccy, ZERO) <= -cap:
        reasons.append(f"daily loss {money(-day[ccy]):,f} {ccy} has reached the maximum {cap:,f}")

    def resting(instrument: str, side: str) -> Decimal:
        return sum((o["remaining"] for o in others if o["instrument_id"] == instrument and o["side"] == side), ZERO)

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
        committed = sum((cost(o["remaining"], Decimal(o["price"]), marks[o["instrument_id"]])
                         for o in others if o["side"] == "buy" and o["instrument_id"] in marks
                         and marks[o["instrument_id"]].currency == ccy), ZERO)
        left = ledger.balance(conn, mm, ccy) - committed - cost(order.size, order.price, mark)
        if left < floor:
            reasons.append(f"{ccy} cash would fall to {money(left):,f} if every bid fills; the reserve is {floor:,f}")
    return reasons


def enforce_daily_loss(conn: sqlite3.Connection, marks: dict[str, Mark],
                       now: datetime | None = None) -> str | None:
    """If any currency's loss for the day (from the ledger) has reached its limit, pause the
    whole bot (cancelling every order) and return why; otherwise None. Called before every
    quoting round and every order."""
    lim = controls.limits(conn)
    for ccy, value in sorted(daily.daily_pnl(conn, marks, now or datetime.now(timezone.utc)).items()):
        cap = lim.get(("max_daily_loss", ccy))
        if value is not None and cap is not None and value <= -cap:
            reason = f"daily loss {money(-value):,f} {ccy} has reached the maximum {cap:,f}"
            if controls.state(conn)["state"] == "running":
                controls.pause(conn, "risk check", reason, now)
            return reason
    return None


class LedgerRiskCheck:
    """The default risk check: limits and state from the database, daily P&L from the ledger."""

    name = "limits from the database, daily loss from the ledger"

    def check(self, conn, order, marks, now, pending=(), replacing=frozenset()) -> list[str]:
        return check(conn, order, marks, now, pending=pending, replacing=replacing)

    def after_refusal(self, conn, marks, now) -> bool:
        return enforce_daily_loss(conn, marks, now) is not None


DEFAULT_RISK = LedgerRiskCheck()


def place_order(conn: sqlite3.Connection, order: Order, marks: dict[str, Mark],
                now: datetime | None = None, risk: RiskCheck | None = None) -> Decision:
    """Check, then record the order as open and send it to the active venue, or audit the
    refusal. All or nothing: if the venue refuses, the order isn't recorded either."""
    risk = risk or DEFAULT_RISK
    now = now or datetime.now(timezone.utc)
    at = now.isoformat()
    with atomic(conn):
        row = conn.execute("SELECT instrument_id, side, price, size FROM mm_orders WHERE id = ?", (order.id,)).fetchone()
        if row:
            if (row["instrument_id"], row["side"], Decimal(row["price"]), Decimal(row["size"])) != (
                    order.instrument_id, order.side, order.price, order.size):
                raise controls.ControlError(f"order {order.id} already placed with different terms")
            return Decision(True, already_placed=True)
        reasons = risk.check(conn, order, marks, now)
        venue = active_venue()
        payload = {"id": order.id, "instrument": order.instrument_id, "side": order.side,
                   "price": plain(order.price), "size": plain(order.size), "venue": venue.name}
        if reasons:
            breach = risk.after_refusal(conn, marks, now)
            record_event(conn, "order_refused", {**payload, "reasons": reasons}, entity=order.instrument_id)
            return Decision(False, tuple(reasons), paused=breach)
        conn.execute("INSERT INTO mm_orders(id, instrument_id, side, price, size, status, created_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                     (order.id, order.instrument_id, order.side, plain(order.price), plain(order.size), at, at))
        record_event(conn, "order_placed", payload, entity=order.instrument_id,
                     idempotency_key=f"order_placed:{order.id}")
        venue.submit(conn, order, now)  # last: a venue that refuses rolls the whole order back
    return Decision(True)


def book_fill(conn: sqlite3.Connection, fill_id: str, bond, side: str, price: Decimal, size: Decimal,
              fee: Decimal, counterparty: str, fee_to: str, on, now: datetime, order_id: str | None = None,
              memo: str = "") -> ledger.Settlement:
    """Book a fill reported by any venue, the same way for all: a delivery-versus-payment
    ledger trade at the clean price (plus accrued, or flat), the fee from the market maker to
    `fee_to`, and the order's filled amount. `side` is the market maker's. Idempotent by fill id
    through the ledger's transaction ids; call it inside the caller's transaction."""
    mm = market_maker_id(conn)
    buyer, seller = (mm, counterparty) if side == "buy" else (counterparty, mm)
    s = ledger.trade(conn, f"fill:{fill_id}", bond, buyer, seller, size, price, on, memo=memo)
    if fee:
        ledger.post(conn, f"fee:{fill_id}", "fee", on, [ledger.Line(mm, bond.currency, -fee),
                                                        ledger.Line(fee_to, bond.currency, fee)],
                    memo=f"fee on {fill_id}" + (f" ({memo})" if memo else ""))
    if order_id:
        row = conn.execute("SELECT size, filled FROM mm_orders WHERE id = ?", (order_id,)).fetchone()
        filled = Decimal(row["filled"]) + size
        conn.execute("UPDATE mm_orders SET filled = ?, status = ?, updated_at = ? WHERE id = ?",
                     (plain(filled), "filled" if filled >= Decimal(row["size"]) else "open", now.isoformat(), order_id))
    return s
