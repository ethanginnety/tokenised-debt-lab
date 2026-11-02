"""Pause, resume, the kill switch and its reset, for the paper bot. Persisted, as in M4.

Pause and kill cancel every open paper order (each cancel charged its network fee, as a
live cancel would be). The kill switch stays on across restarts until an explicit reset,
with a reason, which leaves the bot paused: running again is a separate decision. A
daily-loss breach on either account pauses the bot (both accounts quote the same quotes).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from debtlab.xrpl_market.paper import account
from debtlab.xrpl_market.paper.db import ControlError, limits, set_state, state, tx
from debtlab.xrpl_market.paper.market import Market
from debtlab.xrpl_market.paper.venue import cancel_all, venues


def resume(conn: sqlite3.Connection, by: str, reason: str, now: datetime) -> None:
    with tx(conn):
        cur = state(conn)["state"]
        if cur == "killed":
            raise ControlError("the kill switch is on: reset it (with a reason) before resuming")
        if cur != "running":
            set_state(conn, "running", by, reason, now)


def pause(conn: sqlite3.Connection, by: str, reason: str, now: datetime, market: Market | None) -> int:
    with tx(conn):
        n = cancel_all(conn, f"paused: {reason}", now, venues(market))
        if state(conn)["state"] == "running":
            set_state(conn, "paused", by, reason, now)
    return n


def kill(conn: sqlite3.Connection, by: str, reason: str, now: datetime, market: Market | None) -> int:
    with tx(conn):
        n = cancel_all(conn, f"kill switch: {reason}", now, venues(market))
        if state(conn)["state"] != "killed":
            set_state(conn, "killed", by, reason, now)
    return n


def reset(conn: sqlite3.Connection, by: str, reason: str, now: datetime) -> None:
    if not reason.strip():
        raise ControlError("resetting the kill switch needs a reason")
    with tx(conn):
        if state(conn)["state"] != "killed":
            raise ControlError("the kill switch is not on")
        set_state(conn, "paused", by, f"kill switch reset: {reason}", now)


def enforce_daily_loss(conn: sqlite3.Connection, m: Market | None, now: datetime, cfg: dict) -> str | None:
    """If either account's loss today has reached the limit, pause the bot and say why."""
    if m is None:
        return None
    cap = limits(conn).get("max_daily_loss_eur")
    if cap is None:
        return None
    for a in account.accounts(conn):
        day = account.daily_pnl_eur(conn, a["name"], m, now)
        if day is not None and day <= -cap:
            why = f"{a['name']} account: daily loss EUR {-day:.4f} has reached the maximum {cap}"
            if state(conn)["state"] == "running":
                pause(conn, "risk check", why, now, m)
            return why
    return None
