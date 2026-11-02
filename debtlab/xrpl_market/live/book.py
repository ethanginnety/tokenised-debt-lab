"""Keeping the bot's books to what validated ledgers say (M8). The ledger is the source of truth.

`sync` reads each validated transaction once, in order, and:

    journals fills, fees and external moves; updates orders and transactions
    settles a pending transaction once it appears, or once its last ledger has passed
    marks orders expired once their Expiration has reached the latest close time
    returns every way the ledger disagrees with the books

P&L walks the journal as the paper account does, so the parts add up exactly:
spread + fees + inventory = total, with external moves taken out of the total: a faucet
top-up isn't profit. A fill that took liquidity counts in spread like any other fill, at the
price it actually got.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from debtlab.money import D, ZERO, plain
from debtlab.xrpl_market.live import LABEL, reconcile
from debtlab.xrpl_market.live.db import ControlError, event, iso, limits
from debtlab.xrpl_market.live.ledger import AccountState, Header
from debtlab.xrpl_market.live.reconcile import Effect


def journal(conn: sqlite3.Connection, at: datetime, ledger: int, kind: str, xrp: Decimal, rlusd: Decimal, mid: Decimal,
            ref: str, memo: str = "") -> None:
    conn.execute("INSERT OR IGNORE INTO live_journal(at, ledger_index, kind, xrp, rlusd, mid, ref, memo)"
                 " VALUES (?,?,?,?,?,?,?,?)", (iso(at), ledger, kind, plain(xrp), plain(rlusd), plain(mid), ref, memo))


def balances(conn: sqlite3.Connection) -> tuple[Decimal, Decimal]:
    xrp = rlusd = ZERO
    for r in conn.execute("SELECT xrp, rlusd FROM live_journal"):
        xrp += D(r["xrp"])
        rlusd += D(r["rlusd"])
    return xrp, rlusd


def last_mid(conn: sqlite3.Connection) -> Decimal:
    """The latest price in the journal: for booking when the monitor is down (the kill switch still works)."""
    r = conn.execute("SELECT mid FROM live_journal ORDER BY id DESC LIMIT 1").fetchone()
    return D(r["mid"]) if r else ZERO


def capital_eur(conn: sqlite3.Connection) -> Decimal:
    """What has been put in: the opening value plus every outside move, at the prices then, in EUR."""
    a = account(conn)
    if a is None:
        return ZERO
    total = sum((D(r["xrp"]) * D(r["mid"]) + D(r["rlusd"]) for r in conn.execute(
        "SELECT xrp, rlusd, mid FROM live_journal WHERE kind IN ('open', 'external')")), ZERO)
    return total / D(a["usd_per_eur"])


def account(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM live_account WHERE id = 1").fetchone()


def cursor(conn: sqlite3.Connection) -> int | None:
    r = conn.execute("SELECT ledger_index FROM live_cursor WHERE id = 1").fetchone()
    return None if r is None else r["ledger_index"]


def set_cursor(conn: sqlite3.Connection, ledger: int) -> None:
    conn.execute("INSERT INTO live_cursor(id, ledger_index) VALUES (1, ?) ON CONFLICT(id) DO UPDATE"
                 " SET ledger_index = excluded.ledger_index", (ledger,))


def open_orders(conn: sqlite3.Connection, side: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM live_orders WHERE status IN ('pending', 'open')", []
    if side:
        q += " AND side = ?"
        args.append(side)
    return [{**dict(r), "remaining": D(r["size"]) - D(r["filled_xrp"])}
            for r in conn.execute(q + " ORDER BY placed_at, id", args)]


def open_account(conn: sqlite3.Connection, st: AccountState, mid: Decimal, usd_per_eur: Decimal, now: datetime) -> None:
    """Start the books from the account's first validated read. Refused if it's over the total limit."""
    if account(conn):
        return
    cap = limits(conn).get("max_total_eur")
    eur = (st.xrp * mid + st.rlusd) / usd_per_eur
    if cap is None or eur > cap:
        raise ControlError(f"the account is worth EUR {eur:.2f} at the testnet mid, over the total limit EUR {cap}: "
                           "reduce it, or change the limit yourself, with a reason")
    if not st.has_trust_line:
        raise ControlError(f"account {st.address} has no RLUSD trust line to the verified issuer")
    conn.execute("INSERT INTO live_account(id, address, opened_at, ledger_index, mid, usd_per_eur) VALUES (1,?,?,?,?,?)",
                 (st.address, iso(now), st.ledger_index, plain(mid), plain(usd_per_eur)))
    journal(conn, now, st.ledger_index, "open", st.xrp, st.rlusd, mid, "open", f"first read, ledger {st.ledger_index}")
    set_cursor(conn, st.ledger_index)
    event(conn, now, "account_opened", {"address": st.address, "xrp": plain(st.xrp), "rlusd": plain(st.rlusd),
                                        "mid": plain(mid), "label": LABEL})


def order_by_sequence(conn: sqlite3.Connection, seq: int) -> sqlite3.Row | None:
    """A Sequence can be used again after a send that never left: the one that reached a ledger counts."""
    return conn.execute("SELECT * FROM live_orders WHERE sequence = ? ORDER BY applied_ledger IS NULL, rowid DESC LIMIT 1",
                        (seq,)).fetchone()


def _close(conn: sqlite3.Connection, oid: str, status: str, reason: str, ledger: int, now: datetime) -> None:
    conn.execute("UPDATE live_orders SET status = ?, reason = ?, closed_at = ?, closed_ledger = ? WHERE id = ?",
                 (status, reason, iso(now), ledger, oid))


def _fill(conn: sqlite3.Connection, e: Effect, order: sqlite3.Row | None, role: str, side: str, xrp: Decimal, rlusd: Decimal,
          mid: Decimal, now: datetime) -> None:
    oid = order["id"] if order else None
    fid = f"{e.hash}:{role}:{order['sequence'] if order else side}"
    conn.execute("INSERT OR IGNORE INTO live_fills(id, order_id, ledger_index, at, tx_hash, role, side, xrp, rlusd, mid)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?)", (fid, oid, e.ledger_index, iso(now), e.hash, role, side, plain(xrp),
                                                   plain(rlusd), plain(mid)))
    dx, dr = (xrp, -rlusd) if side == "buy" else (-xrp, rlusd)
    journal(conn, now, e.ledger_index, f"{role}_fill", dx, dr, mid, fid, f"{side} {plain(xrp)} XRP for {plain(rlusd)} RLUSD")
    if order:
        conn.execute("UPDATE live_orders SET filled_xrp = ?, filled_rlusd = ?, took = ? WHERE id = ?",
                     (plain(D(order["filled_xrp"]) + xrp), plain(D(order["filled_rlusd"]) + rlusd),
                      plain(D(order["took"]) + (xrp if role == "taker" else ZERO)), oid))
    event(conn, now, f"{role}_fill", {"id": fid, "order": oid, "side": side, "xrp": plain(xrp), "rlusd": plain(rlusd),
                                      "mid": plain(mid), "ledger": e.ledger_index, "tx": e.hash, "label": LABEL})


def apply(conn: sqlite3.Connection, e: Effect, mid: Decimal, now: datetime) -> None:
    """Book one validated transaction. Idempotent: the journal's refs are unique."""
    if e.ours and e.fee_xrp:
        journal(conn, now, e.ledger_index, "fee", -e.fee_xrp, ZERO, mid, e.hash, f"{e.tx_type} {e.result}")
    if e.ours:
        conn.execute("UPDATE live_txs SET status = ?, result = ?, ledger_index = ?, resolved_at = ? WHERE hash = ?",
                     ("applied" if e.result == "tesSUCCESS" else "failed", e.result, e.ledger_index, iso(now), e.hash))
    for f in e.maker_fills:
        _fill(conn, e, order_by_sequence(conn, f.sequence), "maker", f.side, f.xrp, f.rlusd, mid, now)

    mine = conn.execute("SELECT * FROM live_orders WHERE tx_hash = ?", (e.hash,)).fetchone() if e.ours else None
    if e.took:
        side = "buy" if e.taker_xrp > 0 else "sell"
        _fill(conn, e, mine, "taker", side, abs(e.taker_xrp), abs(e.taker_rlusd), mid, now)
    if e.external:
        journal(conn, now, e.ledger_index, "external", e.external_xrp, e.external_rlusd, mid, e.hash,
                f"{e.tx_type} from outside the bot")
        event(conn, now, "external_change", {"tx": e.hash, "type": e.tx_type, "xrp": plain(e.external_xrp),
                                             "rlusd": plain(e.external_rlusd), "ledger": e.ledger_index})

    if mine is not None:
        if e.result != "tesSUCCESS":
            _close(conn, mine["id"], "failed", e.result, e.ledger_index, now)
        elif e.created is not None:
            conn.execute("UPDATE live_orders SET status = 'open', applied_ledger = ? WHERE id = ?", (e.ledger_index, mine["id"]))
        else:
            _close(conn, mine["id"], "filled", "filled on arrival: it took liquidity", e.ledger_index, now)
            conn.execute("UPDATE live_orders SET applied_ledger = ? WHERE id = ?", (e.ledger_index, mine["id"]))

    for seq in e.removed:
        o = order_by_sequence(conn, seq)
        if o is None or o["status"] not in ("pending", "open"):
            continue
        fresh = conn.execute("SELECT size, filled_xrp FROM live_orders WHERE id = ?", (o["id"],)).fetchone()
        if D(fresh["filled_xrp"]) >= D(fresh["size"]):
            _close(conn, o["id"], "filled", "fully filled", e.ledger_index, now)
        else:
            _close(conn, o["id"], "cancelled", f"removed by {e.tx_type}", e.ledger_index, now)
    for f in e.maker_fills:
        o = order_by_sequence(conn, f.sequence)
        if o is not None and o["status"] in ("pending", "open") and f.sequence not in e.removed:
            if D(o["filled_xrp"]) >= D(o["size"]):
                _close(conn, o["id"], "filled", "fully filled", e.ledger_index, now)


def settle_lapsed(conn: sqlite3.Connection, header: Header, now: datetime) -> list[str]:
    """Pending transactions whose last ledger has passed and that no validated ledger shows: they can never apply."""
    lapsed = []
    for t in conn.execute("SELECT * FROM live_txs WHERE status = 'pending' AND last_ledger < ?", (header.index,)).fetchall():
        conn.execute("UPDATE live_txs SET status = 'not_applied', resolved_at = ? WHERE hash = ?", (iso(now), t["hash"]))
        if t["order_id"] and t["kind"] == "OfferCreate":
            _close(conn, t["order_id"], "failed", "never reached a validated ledger", header.index, now)
        event(conn, now, "tx_not_applied", {"hash": t["hash"], "kind": t["kind"], "sequence": t["sequence"],
                                            "last_ledger": t["last_ledger"]})
        lapsed.append(t["hash"])
    return lapsed


def expire(conn: sqlite3.Connection, header: Header, now: datetime) -> int:
    """Orders whose Expiration has reached the latest close: no later ledger can execute them."""
    n = 0
    for o in conn.execute("SELECT id FROM live_orders WHERE status = 'open' AND expiration <= ?", (header.close_time,)).fetchall():
        _close(conn, o["id"], "expired", "reached its Expiration", header.index, now)
        n += 1
    return n


def sync(conn: sqlite3.Connection, txs: list[dict], st: AccountState, header: Header, mid: Decimal, rlusd_hex: str,
         issuer: str, now: datetime) -> tuple[list[Effect], list[str]]:
    """Bring the books up to this validated read. Returns what happened, and every disagreement left."""
    effects = [reconcile.effect(t, st.address, rlusd_hex, issuer) for t in txs]
    for e in effects:
        apply(conn, e, mid, now)
    settle_lapsed(conn, header, now)
    expire(conn, header, now)
    set_cursor(conn, header.index)
    xrp, rlusd = balances(conn)
    alive = {o["sequence"] for o in open_orders(conn)}
    # The bot is the only signer: an offer whose order just lapsed in the books but which the
    # ledger still holds would show here as unknown, which is what should happen.
    return effects, reconcile.problems(st, xrp, rlusd, alive, header.close_time)


# --- value and P&L --------------------------------------------------------------------------

def value(xrp: Decimal, rlusd: Decimal, mid: Decimal) -> Decimal:
    return xrp * mid + rlusd


@dataclass(frozen=True)
class LivePnl:
    start_value: Decimal
    value: Decimal
    external: Decimal
    total: Decimal
    spread: Decimal
    fees: Decimal
    inventory: Decimal
    maker_fills: int
    taker_fills: int
    fees_paid_xrp: Decimal

    def as_dict(self) -> dict:
        return {k: (plain(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def pnl(conn: sqlite3.Connection, mid_now: Decimal) -> LivePnl:
    rows = conn.execute("SELECT * FROM live_journal ORDER BY id").fetchall()
    if not rows or rows[0]["kind"] != "open":
        raise ControlError("the account isn't open")
    o = rows[0]
    xrp, rlusd, prev = D(o["xrp"]), D(o["rlusd"]), D(o["mid"])
    start = value(xrp, rlusd, prev)
    spread = fees = inventory = ext = fees_xrp = ZERO
    maker = taker = 0
    for r in rows[1:]:
        mid, dx, dr = D(r["mid"]), D(r["xrp"]), D(r["rlusd"])
        inventory += xrp * (mid - prev)
        if r["kind"] in ("maker_fill", "taker_fill"):
            spread += dx * mid + dr
            maker += r["kind"] == "maker_fill"
            taker += r["kind"] == "taker_fill"
        elif r["kind"] == "fee":
            fees += dx * mid + dr
            fees_xrp -= dx
        else:
            ext += dx * mid + dr
        xrp, rlusd, prev = xrp + dx, rlusd + dr, mid
    inventory += xrp * (mid_now - prev)
    now_value = value(xrp, rlusd, mid_now)
    return LivePnl(start, now_value, ext, now_value - start - ext, spread, fees, inventory, maker, taker, fees_xrp)


def record_day_open(conn: sqlite3.Connection, mid: Decimal, now: datetime) -> None:
    xrp, rlusd = balances(conn)
    conn.execute("INSERT OR IGNORE INTO live_day_open(day, at, mid, value_rlusd) VALUES (?,?,?,?)",
                 (now.date().isoformat(), iso(now), plain(mid), plain(value(xrp, rlusd, mid))))


def daily_pnl_rlusd(conn: sqlite3.Connection, mid: Decimal, now: datetime) -> Decimal | None:
    """Since the first read of this UTC day, with external moves taken out."""
    r = conn.execute("SELECT value_rlusd, at FROM live_day_open WHERE day = ?", (now.date().isoformat(),)).fetchone()
    if r is None:
        return None
    xrp, rlusd = balances(conn)
    ext = sum((D(e["xrp"]) * D(e["mid"]) + D(e["rlusd"]) for e in conn.execute(
        "SELECT xrp, rlusd, mid FROM live_journal WHERE kind = 'external' AND at >= ?", (r["at"],))), ZERO)
    return value(xrp, rlusd, mid) - D(r["value_rlusd"]) - ext
