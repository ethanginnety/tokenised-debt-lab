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

Not bond-specific (M7 step 5). An `Order` is a price per unit of the instrument's own
quote convention and a size in its own units: for a bond, a clean price per 100 face and
a face amount; for RLUSD/XRP, RLUSD per XRP and XRP. A `Mark` is whatever a risk check
needs to know about an instrument now, with at least its currency: the bond check's
`execution.Mark` carries accrued and DV01, the RLUSD/XRP paper check's mark the live book.
A strategy's market inputs differ by market too (see `Strategy.decide`).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class Order:
    id: str  # deterministic: placing the same order again does nothing
    instrument_id: str
    side: str  # buy | sell, from the market maker's side
    price: Decimal  # per unit, in the instrument's quote convention (bonds: clean, per 100 face)
    size: Decimal  # in the instrument's units (bonds: face)


@runtime_checkable
class Mark(Protocol):
    """What a risk check needs to know about one instrument now. Each check defines the rest."""
    currency: str


@runtime_checkable
class Strategy(Protocol):
    name: str

    def decide(self, conn: sqlite3.Connection, *market: Any) -> list[Any]:
        """Decide and record a quote, or none with reasons, for every instrument. Places nothing.

        The market inputs depend on the market: bonds `(instruments, curves, on, now)`
        (`quoting.QuoteModelStrategy`); RLUSD/XRP `(market, now)` (`xrpl_market.paper.strategy`)."""
        ...


@runtime_checkable
class RiskCheck(Protocol):
    name: str

    def check(self, conn: sqlite3.Connection, order: Order, marks: Mapping[str, Any], now: datetime,
              pending: tuple[Order, ...] = (), replacing: frozenset[str] = frozenset()) -> list[str]:
        """Every reason the order can't be placed now; empty if it can. Reads, never writes
        (except fixing the day's opening position, the first time it is needed)."""
        ...

    def after_refusal(self, conn: sqlite3.Connection, marks: Mapping[str, Any], now: datetime) -> bool:
        """React to a refusal (e.g. pause the bot on a daily-loss breach). True if it paused."""
        ...


@runtime_checkable
class Venue(Protocol):
    name: str
    simulated: bool  # anything not simulated must be labelled live on every page and log

    def submit(self, conn: sqlite3.Connection, order: Order, now: datetime) -> None:
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

    def submit(self, conn: sqlite3.Connection, order: Order, now: datetime) -> None:
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
