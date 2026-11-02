"""Ledger close times and how long a self-expiring offer can still execute (M8). Pure functions.

A ledger's close time is rounded, currently to 10 s, then forced to increase by at least
1 s, so close times bunch just after each 10 s boundary (:00, :01, :02, then :10). An offer
is expired once its Expiration is at or before the PREVIOUS ledger's close time. So the
first ledger whose close time reaches the Expiration can still execute the offer, and it
is the last that can. Only a ledger that closes can execute anything, so a halted network
can't fill an offer either.

Expirations sit on a 10 s boundary and are worked out from the latest validated close
time, not this machine's clock. The worst case is a measurement, not a guarantee, so a
resolution other than the one measured, or a ledger closed without consensus on its time,
means the budget no longer holds: stop quoting.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal


def expiration_for(close_time: int, cfg: dict) -> int:
    """The latest 10 s boundary at most `expiry_ahead_seconds` after this close time.

    `close_time`: the latest validated ledger's, in seconds (Ripple or Unix: both epochs
    are multiples of 10, so a boundary is a boundary in either)."""
    step = int(cfg["close_time_resolution_seconds"])
    return (int(close_time) + int(cfg["expiry_ahead_seconds"])) // step * step


def worst_case_live_seconds(cfg: dict) -> Decimal:
    """From receiving a ledger to the last moment an offer priced from it can execute, on
    the measured worst cases."""
    return (Decimal(int(cfg["expiry_ahead_seconds"])) + Decimal(cfg["last_execution_after_expiry_seconds"])
            + Decimal(cfg["close_time_ahead_of_receipt_seconds"]))


def budget_holds(resolution: int, close_flags: int, cfg: dict) -> bool:
    """False if this ledger's close time isn't the kind the worst case was measured on."""
    return resolution == int(cfg["close_time_resolution_seconds"]) and close_flags == 0


def boundary_offsets(rows: Iterable[tuple[int, float, float]], step: int = 10) -> list[float]:
    """For each 10 s boundary E: when the last ledger able to execute an offer expiring at E
    was seen validated, minus E. `rows`: (ledger index, close time, received at), in ledger
    order and the same epoch; only unbroken runs of ledgers count."""
    out: list[float] = []
    prev = None
    for row in rows:
        if prev is not None and row[0] == prev[0] + 1:
            first = int(prev[1]) // step * step + step
            out.extend(row[2] - e for e in range(first, int(row[1]) + 1, step))
        prev = row
    return out
