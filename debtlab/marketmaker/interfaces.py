"""Strategy, risk check and venue: the three seams the paper and live adapters plug into (M4 step 7).

    Strategy   decides each round's quotes, recording every decision with its reasons.
               Default: `quoting.QuoteModelStrategy` (the approved quote model).
    RiskCheck  the one check every order passes before it goes anywhere, and what happens
               after a refusal (a daily-loss breach pauses the bot).
               Default: `execution.LedgerRiskCheck` (limits from the database, daily P&L
               from the ledger).
    Venue      where orders go. An order reaches it only after the risk check has passed
               and the order is recorded in the bot's own book (`mm_orders`), in the same
               transaction, so a venue that fails leaves nothing behind. Every cancel
               (requote, pause, kill switch, a daily-loss breach) is sent to the active
               venue, so the kill switch reaches the venue whatever the caller.
               Fills the venue reports are booked one way for every venue:
               `execution.book_fill` (a ledger trade plus the fee, and the order's filled
               amount). Default: `SimulatedVenue`.

The XRPL testnet (M6), paper (M7) and live (M8) adapters implement Venue, and the
live loop runs `use_venue(...)` around its work. Nothing in M4 talks to a real venue.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING, Iterator, Protocol, runtime_checkable

if TYPE_CHECKING:
    from debtlab.instruments import Instrument
    from debtlab.marketdata.models import CurveSnapshot
    from debtlab.marketmaker.execution import Mark, Order
    from debtlab.marketmaker.quoting import QuoteDecision


@runtime_checkable
class Strategy(Protocol):
    name: str

    def decide(self, conn: sqlite3.Connection, instruments: list["Instrument"],
               curves: dict[str, "CurveSnapshot"], on: date, now: datetime) -> list["QuoteDecision"]:
        """Decide and record a quote, or none with reasons, for every instrument. Places nothing."""
        ...


@runtime_checkable
class RiskCheck(Protocol):
    name: str

    def check(self, conn: sqlite3.Connection, order: "Order", marks: dict[str, "Mark"], now: datetime,
              pending: tuple["Order", ...] = (), replacing: frozenset[str] = frozenset()) -> list[str]:
        """Every reason the order can't be placed now; empty if it can. Reads, never writes
        (except fixing the day's opening position, the first time it is needed)."""
        ...

    def after_refusal(self, conn: sqlite3.Connection, marks: dict[str, "Mark"], now: datetime) -> bool:
        """React to a refusal (e.g. pause the bot on a daily-loss breach). True if it paused."""
        ...


@runtime_checkable
class Venue(Protocol):
    name: str
    simulated: bool  # anything not simulated must be labelled live on every page and log

    def submit(self, conn: sqlite3.Connection, order: "Order", now: datetime) -> None:
        """Send an order that has passed the risk check. Raise to refuse it: the order is
        then not recorded either."""
        ...

    def cancel(self, conn: sqlite3.Connection, order_ids: list[str], reason: str, now: datetime) -> None:
        """Cancel these orders at the venue. They are already cancelled in the bot's own book."""
        ...


class SimulatedVenue:
    """The simulated market. It reads the bot's open orders straight from its book, so there
    is nothing to send: submitting and cancelling are recorded locally, and that is all."""

    name = "simulated market"
    simulated = True

    def submit(self, conn: sqlite3.Connection, order: "Order", now: datetime) -> None:
        return None

    def cancel(self, conn: sqlite3.Connection, order_ids: list[str], reason: str, now: datetime) -> None:
        return None


_active: list[Venue] = [SimulatedVenue()]


def active_venue() -> Venue:
    return _active[-1]


@contextmanager
def use_venue(venue: Venue) -> Iterator[Venue]:
    """Make `venue` the one orders and cancels go to, for the duration."""
    if not isinstance(venue, Venue):
        raise TypeError(f"{venue!r} doesn't implement Venue")
    _active.append(venue)
    try:
        yield venue
    finally:
        _active.remove(venue)
