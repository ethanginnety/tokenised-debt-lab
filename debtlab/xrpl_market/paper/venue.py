"""PaperVenue (a `Venue`), and placing and cancelling paper orders. PAPER.

Nothing is sent anywhere. Submitting or cancelling a paper order charges the paper
account the network fee a live OfferCreate or OfferCancel would cost (from the monitor's
latest read of the ledger), at the mid then. Fills come from `bot`, under the account's
fill rule.

`place` mirrors M4's `execution.place_order`: idempotent by order id; the risk check
first; a refusal is audited (repeats of the same refusal are counted, not re-logged) and
may pause the bot; then the order is recorded and submitted in one transaction, so a
venue that fails leaves nothing behind.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from debtlab.money import D, plain
from debtlab.xrpl_market.paper import LABEL, PAIR, account
from debtlab.xrpl_market.paper.db import ControlError, event, iso, tx
from debtlab.xrpl_market.paper.market import Market
from debtlab.xrpl_market.paper.risk import PaperOrder, PaperRiskCheck


class PaperVenue:
    simulated = True        # not live: nothing reaches a ledger
    label = LABEL

    def __init__(self, account_name: str, market: Market | None = None):
        self.account = account_name
        self.name = f"paper ({account_name} fill rule)"
        self.market = market

    def submit(self, conn: sqlite3.Connection, order: PaperOrder, now: datetime) -> None:
        if order.account != self.account:
            raise ControlError(f"order {order.id} is for the {order.account} account, not {self.account}")
        self._fee(conn, f"place:{order.id}", "OfferCreate", now)

    def cancel(self, conn: sqlite3.Connection, order_ids: list[str], reason: str, now: datetime) -> None:
        for oid in order_ids:
            self._fee(conn, f"cancel:{oid}", f"OfferCancel ({reason})", now)

    def _fee(self, conn: sqlite3.Connection, ref: str, memo: str, now: datetime) -> None:
        m = self.market
        mid = m.mid if m and m.mid is not None else _last_mid(conn, self.account)
        fee = m.fee_xrp if m else D("0.00001")
        account.journal(conn, self.account, now, m.ledger_index if m else None, "fee", -fee, Decimal(0), mid, ref,
                        f"network fee, {memo}")


def _last_mid(conn: sqlite3.Connection, acct: str) -> Decimal:
    r = conn.execute("SELECT mid FROM paper_journal WHERE account = ? ORDER BY id DESC LIMIT 1", (acct,)).fetchone()
    return D(r["mid"])


def venues(market: Market | None) -> dict[str, PaperVenue]:
    from debtlab.xrpl_market.paper import RULES
    return {name: PaperVenue(name, market) for name in RULES}


def place(conn: sqlite3.Connection, order: PaperOrder, market: Market, now: datetime, risk: PaperRiskCheck,
          venue: PaperVenue, decision_id: int | None = None, refusals: dict | None = None) -> list[str]:
    """Check, then record and submit. Returns the reasons it was refused (empty if placed)."""
    with tx(conn):
        row = conn.execute("SELECT side, price, size, account FROM paper_orders WHERE id = ?", (order.id,)).fetchone()
        if row:
            if (row["side"], D(row["price"]), D(row["size"]), row["account"]) != (
                    order.side, order.price, order.size, order.account):
                raise ControlError(f"paper order {order.id} already placed with different terms")
            return []
        reasons = risk.check(conn, order, {PAIR: market}, now)
        payload = {"id": order.id, "account": order.account, "side": order.side, "price": plain(order.price),
                   "size": plain(order.size), "venue": venue.name, "label": LABEL}
        if reasons:
            paused = risk.after_refusal(conn, {PAIR: market}, now)
            key = (order.account, order.side)
            prev = refusals.get(key) if refusals is not None else None
            if prev is not None and prev[0] == tuple(reasons) and not paused:
                prev[1] += 1                     # the same refusal again: counted, not re-logged
            else:
                event(conn, now, "order_refused", {**payload, "reasons": reasons, "paused": paused,
                                                   "previous_refusal_repeated": prev[1] if prev else 0})
                if refusals is not None:
                    refusals[key] = [tuple(reasons), 0]
            return reasons
        if refusals is not None:
            refusals.pop((order.account, order.side), None)
        conn.execute("INSERT INTO paper_orders(id, account, decision_id, side, price, size, status, placed_at,"
                     " placed_ledger) VALUES (?,?,?,?,?,?,'open',?,?)",
                     (order.id, order.account, decision_id, order.side, plain(order.price), plain(order.size),
                      iso(now), market.ledger_index))
        event(conn, now, "order_placed", payload)
        venue.submit(conn, order, now)
    return []


def cancel(conn: sqlite3.Connection, orders: list[dict], reason: str, now: datetime,
           vs: dict[str, PaperVenue], ledger: int | None = None) -> int:
    """Cancel these open orders, each audited, and send the cancels to each account's venue.

    `ledger`: the latest ledger seen when the cancel was decided (default: the venues' market).
    A cancel only reaches a later ledger, so the order still counts for that ledger's trades."""
    if ledger is None:
        m = next(iter(vs.values())).market if vs else None
        ledger = m.ledger_index if m else None
    with tx(conn):
        by_account: dict[str, list[str]] = {}
        for o in orders:
            n = conn.execute("UPDATE paper_orders SET status = 'cancelled', closed_at = ?, closed_ledger = ?, reason = ?"
                             " WHERE id = ? AND status = 'open'", (iso(now), ledger, reason, o["id"])).rowcount
            if n:
                event(conn, now, "order_cancelled", {"id": o["id"], "account": o["account"], "reason": reason})
                by_account.setdefault(o["account"], []).append(o["id"])
        for acct, ids in by_account.items():
            vs[acct].cancel(conn, ids, reason, now)
    return sum(len(v) for v in by_account.values())


def cancel_all(conn: sqlite3.Connection, reason: str, now: datetime, vs: dict[str, PaperVenue],
               ledger: int | None = None) -> int:
    return cancel(conn, account.open_orders(conn), reason, now, vs, ledger)
