"""Pause, resume, the kill switch and its reset, for the testnet venue (M8 step 6).

State is persisted, so a kill survives a restart until an explicit reset, which leaves the
bot paused. `kill` sets the state and commits it BEFORE anything goes on the network, then
works from the ledger's own list of offers, not the bot's records: it runs with the bot
down, and keeps cancelling until the ledger shows none left. If it can't get there, every
offer still expires by itself within the time budget (decision B).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from debtlab.xrpl_market.live import book
from debtlab.xrpl_market.live.db import ControlError, event, set_state, state, tx
from debtlab.xrpl_market.live.ledger import AccountState, Header, TestnetLedger
from debtlab.xrpl_market.live.reconcile import Effect
from debtlab.xrpl_market.live.signer import MAX_FEE_DROPS
from debtlab.xrpl_market.live.venue import Context, LiveVenue, next_sequence, unresolved
from debtlab.xrpl_market.readonly import XrplReadError

ROUND_PAUSE_SECONDS = 2.0


def resume(conn: sqlite3.Connection, by: str, reason: str, now: datetime) -> None:
    with tx(conn):
        cur = state(conn)["state"]
        if cur == "killed":
            raise ControlError("the kill switch is on: reset it (with a reason) before resuming")
        if cur != "running":
            set_state(conn, "running", by, reason, now)


def pause(conn: sqlite3.Connection, by: str, reason: str, now: datetime) -> None:
    """A running bot cancels its offers on its next tick; with the bot down they expire by themselves."""
    with tx(conn):
        if state(conn)["state"] == "running":
            set_state(conn, "paused", by, reason, now)


def reset(conn: sqlite3.Connection, by: str, reason: str, now: datetime) -> None:
    if not reason.strip():
        raise ControlError("resetting the kill switch needs a reason")
    with tx(conn):
        if state(conn)["state"] != "killed":
            raise ControlError("the kill switch is not on")
        set_state(conn, "paused", by, f"kill switch reset: {reason}", now)


def set_killed(conn: sqlite3.Connection, by: str, reason: str, now: datetime) -> None:
    with tx(conn):
        if state(conn)["state"] != "killed":
            set_state(conn, "killed", by, reason, now)


@dataclass(frozen=True)
class Read:
    header: Header
    account: AccountState
    effects: tuple[Effect, ...]
    problems: tuple[str, ...]


def read_ledger(conn: sqlite3.Connection, ledger: TestnetLedger, venue: LiveVenue, cfg: dict, mid: Decimal | None,
                fee_drops: int, now: datetime) -> Read:
    """One validated read: books brought up to it, and the venue's sequence, fee and ledger set from it."""
    h = ledger.latest()
    address = venue.signer.address
    st = ledger.account(address, h.index)
    effects, problems = [], []
    if book.account(conn):
        use_mid = mid if mid is not None else book.last_mid(conn)
        txs = ledger.transactions(address, book.cursor(conn), h.index)
        with tx(conn):
            effects, problems = book.sync(conn, txs, st, h, use_mid, cfg["rlusd"]["currency_hex"], cfg["rlusd"]["issuer"], now)
    venue.context = Context(h, next_sequence(conn, st.sequence), fee_drops, mid if mid is not None else Decimal(0))
    return Read(h, st, tuple(effects), tuple(problems))


def cancel_offers(conn: sqlite3.Connection, venue: LiveVenue, read: Read, reason: str, now: datetime,
                  include_expired: bool = False) -> int:
    """Cancel what the ledger holds and what we've sent that may yet land, unless a cancel is already under way.

    A pending offer can be cancelled by its Sequence: the cancel's is higher, so it lands after it."""
    under_way = {r["offer_sequence"] for r in conn.execute(
        "SELECT offer_sequence FROM live_txs WHERE status = 'pending' AND offer_sequence IS NOT NULL")}
    seqs = {o.sequence for o in read.account.offers
            if include_expired or o.expiration is None or o.expiration > read.header.close_time}
    seqs |= {r["sequence"] for r in conn.execute("SELECT sequence FROM live_orders WHERE status = 'pending'")}
    n = 0
    for seq in sorted(seqs - under_way):
        o = book.order_by_sequence(conn, seq)
        venue.cancel_offer(conn, seq, reason, now, o["id"] if o else None)
        n += 1
    return n


@dataclass(frozen=True)
class Cleared:
    zero: bool          # the ledger shows no offers, and nothing we sent is still unresolved
    sent: int
    remaining: int
    last_error: str | None = None


def cancel_until_zero(conn: sqlite3.Connection, ledger: TestnetLedger, venue: LiveVenue, cfg: dict, reason: str,
                      fee_drops: int, *, timeout: float = 60.0, clock: Callable[[], datetime],
                      sleep: Callable[[float], None] = time.sleep) -> Cleared:
    """Keep cancelling until the ledger's own list is empty. The fee doubles each round, up to the signer's maximum."""
    sent, rounds, remaining, error = 0, 0, -1, None
    started = clock()
    while True:
        now = clock()
        try:
            read = read_ledger(conn, ledger, venue, cfg, None, min(fee_drops * 2 ** rounds, MAX_FEE_DROPS), now)
            remaining, error = len(read.account.offers), None
            if remaining == 0 and unresolved(conn) == 0:
                return Cleared(True, sent, 0)
            sent += cancel_offers(conn, venue, read, reason, now, include_expired=True)
            rounds += 1
        except XrplReadError as e:
            error = str(e)
        if (clock() - started).total_seconds() >= timeout:
            with tx(conn):
                event(conn, clock(), "cancel_incomplete", {"remaining": remaining, "sent": sent, "error": error})
            return Cleared(False, sent, remaining, error)
        sleep(ROUND_PAUSE_SECONDS)


def kill(conn: sqlite3.Connection, ledger: TestnetLedger, venue: LiveVenue, cfg: dict, by: str, reason: str, fee_drops: int,
         *, timeout: float = 60.0, clock: Callable[[], datetime], sleep: Callable[[float], None] = time.sleep) -> Cleared:
    set_killed(conn, by, reason, clock())
    return cancel_until_zero(conn, ledger, venue, cfg, f"kill switch: {reason}", fee_drops, timeout=timeout,
                             clock=clock, sleep=sleep)
