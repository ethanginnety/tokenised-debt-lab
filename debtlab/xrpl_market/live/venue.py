"""The testnet venue: a `Venue` that places, replaces and cancels real offers on TESTNET (M8).

Unlike the paper and simulated venues, a network sits in the middle, so the order of things
differs. The risk check runs and the order and transaction are recorded and COMMITTED before
anything is sent, so a crash can't lose track of one. Then:

    the server refuses it outright (tem, tef, tel)  -> failed; it can never apply
    no connection before sending                    -> failed
    anything else, including no answer              -> pending, until a validated ledger
        shows it or its LastLedgerSequence passes (`book.py` settles it)

Nothing new is sent while a transaction is unresolved, except cancels, so the Sequence is
always known. Every Expiration comes from `closetimes.expiration_for`, on ledger time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from debtlab.marketmaker.interfaces import Order
from debtlab.money import plain
from debtlab.xrpl_market import closetimes
from debtlab.xrpl_market.live import LABEL
from debtlab.xrpl_market.live.db import event, iso, tx
from debtlab.xrpl_market.live.ledger import Header
from debtlab.xrpl_market.live.signer import Signed, Signer, SubmitFailed, Submitter

DROPS = Decimal(1_000_000)
LAST_LEDGER_AHEAD = 3
NEVER_APPLIES = ("tem", "tef", "tel")


@dataclass(frozen=True)
class LiveOrder(Order):
    issuer: str = ""
    decision_id: int | None = None
    paper_order_id: str | None = None
    replaces: int | None = None
    replaces_order: str | None = None


@dataclass(frozen=True)
class Context:
    """What sending needs from the latest validated read."""
    header: Header
    next_sequence: int
    fee_drops: int
    mid: Decimal


def next_sequence(conn: sqlite3.Connection, ledger_sequence: int) -> int:
    """The account's next Sequence: the ledger's, past anything we've sent that may yet apply."""
    r = conn.execute("SELECT MAX(sequence) AS s FROM live_txs WHERE status = 'pending'").fetchone()
    return max(ledger_sequence, (r["s"] + 1) if r["s"] is not None else 0)


def unresolved(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM live_txs WHERE status = 'pending'").fetchone()[0]


class LiveVenue:
    simulated = False       # real transactions on a real ledger, with test funds
    label = LABEL

    def __init__(self, signer: Signer, submitter: Submitter, cfg: dict):
        self.signer, self.submitter, self.expiry = signer, submitter, cfg["offer_expiry"]
        self.name = f"XRPL testnet ({signer.address})"
        self.context: Context | None = None
        self.down: str | None = None     # why the last send lost its connection; the bot stops quoting on it

    def _ctx(self) -> Context:
        if self.context is None:
            raise RuntimeError("the venue has no validated ledger read yet")
        return self.context

    def _send(self, conn: sqlite3.Connection, signed: Signed, order_id: str | None, now: datetime) -> str:
        """Record, commit, then send. Returns the transaction's status now."""
        with tx(conn):
            conn.execute("INSERT INTO live_txs(hash, kind, sequence, last_ledger, offer_sequence, order_id, status,"
                         " created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                         (signed.hash, signed.kind, signed.sequence, signed.last_ledger, signed.offer_sequence, order_id,
                          iso(now)))
        status, prelim, resolved = "pending", None, False
        try:
            prelim = self.submitter.submit(signed).get("engine_result")
            self.down = None
            if (prelim or "").startswith(NEVER_APPLIES):
                status, resolved = "failed", True
        except SubmitFailed as e:
            prelim = self.down = str(e)
            if not e.sent:
                status, resolved = "failed", True
        with tx(conn):
            conn.execute("UPDATE live_txs SET status = ?, preliminary = ?, resolved_at = ? WHERE hash = ?",
                         (status, prelim, iso(now) if resolved else None, signed.hash))
            event(conn, now, "tx_sent", {"hash": signed.hash, "kind": signed.kind, "sequence": signed.sequence,
                                         "offer_sequence": signed.offer_sequence, "preliminary": prelim, "status": status,
                                         "label": LABEL})
        return status

    def submit(self, conn: sqlite3.Connection, order: LiveOrder, now: datetime) -> None:
        """Sign and send one offer the risk check has passed, replacing `order.replaces` in the same transaction."""
        c = self._ctx()
        expiration = closetimes.expiration_for(c.header.close_time, self.expiry)
        signed = self.signer.create_offer(side=order.side, price=order.price, size=order.size, sequence=c.next_sequence,
                                          expiration=expiration, last_ledger=c.header.index + LAST_LEDGER_AHEAD,
                                          fee_drops=c.fee_drops, replaces=order.replaces)
        with tx(conn):
            conn.execute("INSERT INTO live_orders(id, decision_id, paper_order_id, side, price, size, sequence, expiration,"
                         " tx_hash, replaces, status, placed_at, placed_ledger) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                         (order.id, order.decision_id, order.paper_order_id, order.side, plain(order.price), plain(order.size),
                          signed.sequence, expiration, signed.hash, order.replaces, iso(now), c.header.index))
            event(conn, now, "order_placed", {"id": order.id, "side": order.side, "price": plain(order.price),
                                              "size": plain(order.size), "sequence": signed.sequence,
                                              "expiration": expiration, "replaces": order.replaces, "label": LABEL})
        self.context = Context(c.header, c.next_sequence + 1, c.fee_drops, c.mid)
        if self._send(conn, signed, order.id, now) == "failed":
            with tx(conn):
                conn.execute("UPDATE live_orders SET status = 'failed', closed_at = ?, reason = 'not accepted by the server'"
                             " WHERE id = ?", (iso(now), order.id))

    def cancel_offer(self, conn: sqlite3.Connection, offer_sequence: int, reason: str, now: datetime,
                     order_id: str | None = None) -> str:
        c = self._ctx()
        signed = self.signer.cancel_offer(offer_sequence=offer_sequence, sequence=c.next_sequence,
                                          last_ledger=c.header.index + LAST_LEDGER_AHEAD, fee_drops=c.fee_drops)
        self.context = Context(c.header, c.next_sequence + 1, c.fee_drops, c.mid)
        with tx(conn):
            event(conn, now, "cancel_sent", {"offer_sequence": offer_sequence, "reason": reason, "label": LABEL})
        return self._send(conn, signed, order_id, now)

    def cancel(self, conn: sqlite3.Connection, order_ids: list[str], reason: str, now: datetime) -> None:
        """The `Venue` protocol's cancel: every named order that's still alive, by its offer's Sequence."""
        for oid in order_ids:
            r = conn.execute("SELECT sequence FROM live_orders WHERE id = ? AND status IN ('pending', 'open')", (oid,)).fetchone()
            if r:
                self.cancel_offer(conn, r["sequence"], reason, now, oid)
