"""The paper accounts: balances, the XRP reserve, value, the P&L split and markouts.

Two accounts on the same quotes, one per fill rule (decision E). Each starts with
`start_eur` (decision D: EUR 50) at the opening mid, half XRP and half RLUSD; the XRP
reserve a live account would need comes out of the XRP half (it is held, not spent).

Balances are the sums of an append-only journal. Every entry stores the mid at the time,
so the P&L splits exactly, in RLUSD:

    spread captured   each fill against the mid then: buy q at P -> q * (mid - P)
    fees              each network fee at the mid then: -fee * mid
    inventory         XRP held between entries times the change in mid (worked out by
                      walking the journal, not as a remainder)
    total             value now minus value at opening, value = XRP * mid + RLUSD

and spread + fees + inventory = total exactly (tested). Euro figures divide by the ECB
rate (1 RLUSD = 1 USD, decision D, risk R19).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from debtlab.money import D, plain
from debtlab.xrpl_market.paper import RULES
from debtlab.xrpl_market.paper.db import ControlError, event, iso, limits, parse
from debtlab.xrpl_market.paper.market import Market, mid_after

DROP = Decimal("0.000001")
ZERO = Decimal(0)


def journal(conn: sqlite3.Connection, account: str, at: datetime, ledger: int | None, kind: str,
            xrp: Decimal, rlusd: Decimal, mid: Decimal, ref: str, memo: str = "") -> None:
    conn.execute("INSERT INTO paper_journal(account, at, ledger_index, kind, xrp, rlusd, mid, ref, memo)"
                 " VALUES (?,?,?,?,?,?,?,?,?)", (account, iso(at), ledger, kind, plain(xrp), plain(rlusd), plain(mid), ref, memo))


def accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM paper_accounts ORDER BY CASE name WHEN 'conservative' THEN 0 ELSE 1 END").fetchall()


def open_accounts(conn: sqlite3.Connection, m: Market, cfg: dict, now: datetime) -> bool:
    """Open both accounts at this mid, once. False if they are already open."""
    if accounts(conn):
        return False
    if m.problems(D(cfg["paper"]["limits"]["max_data_age_seconds"])):
        raise ControlError("can't open the paper accounts without a fresh book and EUR rate: "
                           + "; ".join(m.problems(D(cfg["paper"]["limits"]["max_data_age_seconds"]))))
    start = D(cfg["paper"]["start_eur"])
    cap = limits(conn).get("max_total_eur")
    if cap is None or start > cap:
        raise ControlError(f"starting value EUR {start} is over the total limit EUR {cap}")
    half = m.from_eur(start / 2)
    xrp = (half / m.mid).quantize(DROP, rounding=ROUND_DOWN)
    rlusd = half.quantize(DROP, rounding=ROUND_DOWN)
    for name in RULES:
        conn.execute("INSERT INTO paper_accounts(name, rule, opened_at, ledger_index, mid, usd_per_eur, start_eur)"
                     " VALUES (?,?,?,?,?,?,?)", (name, name, iso(now), m.ledger_index, plain(m.mid),
                                                  plain(m.fx.usd_per_eur), plain(start)))
        journal(conn, name, now, m.ledger_index, "open", xrp, rlusd, m.mid, "open",
                f"EUR {start}: half XRP, half RLUSD at mid {m.mid}, EUR/USD {m.fx.usd_per_eur} ({m.fx.obs_date})")
    event(conn, now, "accounts_opened", {"xrp": plain(xrp), "rlusd": plain(rlusd), "mid": plain(m.mid),
                                         "usd_per_eur": plain(m.fx.usd_per_eur), "ledger": m.ledger_index})
    return True


def balances(conn: sqlite3.Connection, account: str) -> tuple[Decimal, Decimal]:
    xrp = rlusd = ZERO
    for r in conn.execute("SELECT xrp, rlusd FROM paper_journal WHERE account = ?", (account,)):
        xrp += D(r["xrp"])
        rlusd += D(r["rlusd"])
    return xrp, rlusd


def open_orders(conn: sqlite3.Connection, account: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM paper_orders WHERE status = 'open'", []
    if account:
        q += " AND account = ?"
        args.append(account)
    rows = conn.execute(q + " ORDER BY placed_at, id", args)
    return [{**dict(r), "remaining": D(r["size"]) - D(r["filled"])} for r in rows]


def in_force(conn: sqlite3.Connection, ledger: int) -> list[dict]:
    """Orders that were resting during `ledger`: placed after an earlier ledger, and open or
    cancelled after seeing this ledger or a later one, with something left to fill."""
    rows = conn.execute("SELECT * FROM paper_orders WHERE placed_ledger < ? AND (status = 'open' OR"
                        " (status = 'cancelled' AND closed_ledger >= ?)) ORDER BY placed_at, id", (ledger, ledger))
    out = [{**dict(r), "remaining": D(r["size"]) - D(r["filled"])} for r in rows]
    return [o for o in out if o["remaining"] > 0]


def reserve(m: Market, cfg: dict, offers: int) -> Decimal:
    """XRP a live account must keep: base + per object (the RLUSD trust line, each offer)."""
    return m.reserve_base_xrp + m.reserve_inc_xrp * (cfg["paper"]["trust_lines"] + offers)


def value(xrp: Decimal, rlusd: Decimal, mid: Decimal) -> Decimal:
    return xrp * mid + rlusd


@dataclass(frozen=True)
class Pnl:
    start_value: Decimal
    value: Decimal
    total: Decimal
    spread: Decimal
    fees: Decimal
    inventory: Decimal
    fills: int
    fees_paid_xrp: Decimal

    def as_dict(self) -> dict:
        return {k: (plain(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def pnl(conn: sqlite3.Connection, account: str, mid_now: Decimal) -> Pnl:
    rows = conn.execute("SELECT * FROM paper_journal WHERE account = ? ORDER BY id", (account,)).fetchall()
    if not rows or rows[0]["kind"] != "open":
        raise ControlError(f"paper account {account} is not open")
    o = rows[0]
    xrp, rlusd, prev = D(o["xrp"]), D(o["rlusd"]), D(o["mid"])
    start = value(xrp, rlusd, prev)
    spread = fees = inventory = fees_xrp = ZERO
    fills = 0
    for r in rows[1:]:
        mid, dx, dr = D(r["mid"]), D(r["xrp"]), D(r["rlusd"])
        inventory += xrp * (mid - prev)
        if r["kind"] == "fill":
            spread += dx * mid + dr
            fills += 1
        else:
            fees += dx * mid + dr
            fees_xrp -= dx
        xrp, rlusd, prev = xrp + dx, rlusd + dr, mid
    inventory += xrp * (mid_now - prev)
    now_value = value(xrp, rlusd, mid_now)
    return Pnl(start, now_value, now_value - start, spread, fees, inventory, fills, fees_xrp)


def record_day_open(conn: sqlite3.Connection, m: Market, now: datetime) -> None:
    day = now.date().isoformat()
    for a in accounts(conn):
        xrp, rlusd = balances(conn, a["name"])
        conn.execute("INSERT OR IGNORE INTO paper_day_open(account, day, at, mid, value_rlusd) VALUES (?,?,?,?,?)",
                     (a["name"], day, iso(now), plain(m.mid), plain(value(xrp, rlusd, m.mid))))


def daily_pnl_eur(conn: sqlite3.Connection, account: str, m: Market, now: datetime) -> Decimal | None:
    """The account's P&L since its value at the first tick of this UTC day, in euro at the
    current rate. None if the day hasn't been opened yet or there's no mid or rate."""
    r = conn.execute("SELECT value_rlusd FROM paper_day_open WHERE account = ? AND day = ?",
                     (account, now.date().isoformat())).fetchone()
    if r is None or m.mid is None or m.fx is None:
        return None
    xrp, rlusd = balances(conn, account)
    return m.to_eur(value(xrp, rlusd, m.mid) - D(r["value_rlusd"]))


def markouts(conn: sqlite3.Connection, mon: sqlite3.Connection, account: str, now: datetime,
             horizons: list[int]) -> dict[int, dict]:
    """For each horizon (seconds): the fills' value against the first mid at least that long
    after each fill, in RLUSD (positive = the fill was good), and how many fills it covers so far."""
    out = {h: {"rlusd": ZERO, "fills": 0, "pending": 0} for h in horizons}
    for f in conn.execute("SELECT * FROM paper_fills WHERE account = ? ORDER BY at", (account,)):
        q, p, at = D(f["xrp"]), D(f["price"]), parse(f["at"])
        for h in horizons:
            later = mid_after(mon, at + timedelta(seconds=h), now)
            if later is None:
                out[h]["pending"] += 1
                continue
            mid = later[0]
            out[h]["rlusd"] += q * (mid - p) if f["side"] == "buy" else q * (p - mid)
            out[h]["fills"] += 1
    return out
