"""The simulated holder ledger: who owns what, and every movement, by double entry.

Every movement is a transaction of two or more lines whose amounts sum to zero
for each asset. Paying a $90,000 coupon is one line of -90,000 USD on the
issuer and one of +90,000 USD on the holder; nothing is created or lost.
Balances are never stored: a balance is the sum of the lines up to a date, so
"who held what on the record date" is a query, and history can't be edited.

Assets are currencies (cash, e.g. USD) or instrument ids (units: face amount
for a bond, shares for a fund). Only issuers and the funding account may go
negative: an issuer's negative balance is what it has issued and owes, the
funding account's is the cash brought into the simulation from outside.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from debtlab.audit.db import atomic, record_event
from debtlab.instruments import Instrument
from debtlab.money import D, HUNDRED, ZERO, money
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import accrued_per_100

ISSUER_PREFIX = "issuer:"
CASH_PLACES = 2  # cents
UNIT_PLACES = 4  # bond face to 1/100 cent, fund shares to 1/10,000

# Parties that may hold a negative balance. Everyone else (test holders, the market
# maker, the simulated market) can never go below zero, so nobody sells short.
MAY_GO_NEGATIVE = {"issuer", "funding"}
TRADING_ROLES = {"holder", "market_maker", "market"}


class LedgerError(ValueError):
    """A movement that would break the ledger's rules. Nothing is written."""


@dataclass(frozen=True)
class Line:
    owner: str  # party id
    asset: str  # currency or instrument id
    amount: Decimal  # positive = received, negative = paid away


@dataclass(frozen=True)
class Settlement:
    txn_id: str
    posted: bool  # False if this exact trade was already booked
    face: Decimal
    clean_price: Decimal  # per 100 face, as quoted
    accrued_per_100: Decimal  # zero when the bond trades flat
    flat: bool
    cash: Decimal  # face x (clean + accrued) / 100, to the cent


def issuer_id(instrument_id: str) -> str:
    return ISSUER_PREFIX + instrument_id


def is_cash(asset: str) -> bool:
    return len(asset) == 3 and asset.isalpha() and asset.isupper()


def _canonical(amount: Decimal) -> str:
    return f"{amount.normalize():f}"  # 4000000, not 4E+6


# ---------------------------------------------------------------- parties


def add_party(conn: sqlite3.Connection, party_id: str, name: str, role: str,
              description: str = "", payments_blocked: bool = False) -> bool:
    """Create a holder or issuer once; returns False if it already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO parties(id, name, role, description, payments_blocked, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (party_id, name, role, description, int(payments_blocked), datetime.now(timezone.utc).isoformat()),
    )
    return cur.rowcount == 1


def parties(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name, role, description, payments_blocked FROM parties ORDER BY role, id")
    return [{**dict(r), "payments_blocked": bool(r["payments_blocked"])} for r in rows]


def _roles(conn: sqlite3.Connection, owners: set[str]) -> dict[str, str]:
    marks = ",".join("?" for _ in owners)
    rows = conn.execute(f"SELECT id, role FROM parties WHERE id IN ({marks})", tuple(owners))
    return {r["id"]: r["role"] for r in rows}


# ---------------------------------------------------------------- posting


def post(conn: sqlite3.Connection, txn_id: str, kind: str, effective_date: date,
         lines: list[Line], memo: str = "") -> bool:
    """Write one balanced transaction, all or nothing, and audit it.

    Returns False if this exact transaction is already posted (so retries are
    safe); raises LedgerError if the same id was posted with different contents,
    or if the transaction breaks a rule.
    """
    _validate(lines)
    content = {"kind": kind, "effective_date": effective_date.isoformat(),
               "lines": sorted([ln.owner, ln.asset, _canonical(ln.amount)] for ln in lines)}
    fingerprint = hashlib.sha256(json.dumps(content).encode("utf-8")).hexdigest()

    with atomic(conn):
        row = conn.execute("SELECT fingerprint FROM ledger_txns WHERE id = ?", (txn_id,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise LedgerError(f"transaction {txn_id} already posted with different contents")
            return False
        roles = _roles(conn, {ln.owner for ln in lines})
        unknown = sorted({ln.owner for ln in lines} - set(roles))
        if unknown:
            raise LedgerError(f"unknown party {', '.join(unknown)}")
        for ln in lines:
            if roles[ln.owner] == "funding" and not is_cash(ln.asset):
                raise LedgerError(f"{ln.owner} is a funding account; it moves cash only")
            if roles[ln.owner] not in MAY_GO_NEGATIVE:
                _no_back_dating(conn, ln.owner, ln.asset, effective_date)
        for asset in {ln.asset for ln in lines if not is_cash(ln.asset)}:
            _books_open(conn, asset, effective_date, kind)

        conn.execute(
            "INSERT INTO ledger_txns(id, kind, effective_date, fingerprint, memo, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (txn_id, kind, effective_date.isoformat(), fingerprint, memo, datetime.now(timezone.utc).isoformat()),
        )
        conn.executemany(
            "INSERT INTO ledger_entries(txn_id, owner, asset, amount) VALUES (?, ?, ?, ?)",
            [(txn_id, ln.owner, ln.asset, _canonical(ln.amount)) for ln in lines],
        )
        # No back-dating means the all-time balance is also the balance on every later date.
        for owner, asset in {(ln.owner, ln.asset) for ln in lines if roles[ln.owner] not in MAY_GO_NEGATIVE}:
            if balance(conn, owner, asset) < 0:
                raise LedgerError(f"{owner}'s {asset} balance would go below zero")
        record_event(conn, "ledger_posted", {**content, "id": txn_id, "memo": memo},
                     entity=txn_id, idempotency_key=f"ledger_posted:{txn_id}")
    return True


def _validate(lines: list[Line]) -> None:
    if len(lines) < 2:
        raise LedgerError("a transaction needs at least two lines")
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for ln in lines:
        if ln.amount == 0:
            raise LedgerError("a line can't be zero")
        places = CASH_PLACES if is_cash(ln.asset) else UNIT_PLACES
        if ln.amount != ln.amount.quantize(Decimal(1).scaleb(-places)):
            raise LedgerError(f"{ln.asset} amounts allow at most {places} decimal places, got {ln.amount}")
        totals[ln.asset] += ln.amount
    for asset, total in totals.items():
        if total != 0:
            raise LedgerError(f"transaction does not balance in {asset} (off by {total})")


def _no_back_dating(conn: sqlite3.Connection, owner: str, asset: str, effective: date) -> None:
    row = conn.execute(
        "SELECT MAX(t.effective_date) AS latest FROM ledger_entries e JOIN ledger_txns t ON t.id = e.txn_id"
        " WHERE e.owner = ? AND e.asset = ?", (owner, asset),
    ).fetchone()
    if row["latest"] and effective.isoformat() < row["latest"]:
        raise LedgerError(
            f"{owner}'s {asset} account already has a movement on {row['latest']}; "
            f"a new one can't be dated before it ({effective.isoformat()})"
        )


def _books_open(conn: sqlite3.Connection, instrument_id: str, effective: date, kind: str) -> None:
    """Once a payment is scheduled, holdings on its record date are fixed: no unit movement
    dated on or before it, or who is owed what would no longer match the ledger. Once the
    principal (or a default recovery) is scheduled, the only movement left is redeeming
    units from holders of record. Units can't move after a bond's final record date at all,
    except to be redeemed: principal is paid to, and redeemed from, the holders on that date."""
    row = conn.execute("SELECT MAX(record_date) AS closed, MAX(kind IN ('principal', 'recovery')) AS maturing"
                       " FROM payment_events WHERE instrument_id = ?", (instrument_id,)).fetchone()
    if row["maturing"] and kind != "redemption":
        raise LedgerError(f"{instrument_id} has reached its final payment; its units can only be redeemed")
    final = conn.execute("SELECT final_record_date FROM bond_terms WHERE instrument_id = ?",
                         (instrument_id,)).fetchone()
    if final and kind != "redemption" and effective.isoformat() > final["final_record_date"]:
        raise LedgerError(
            f"{instrument_id} units can't move after its final record date {final['final_record_date']} "
            f"(only be redeemed); a movement can't be dated {effective.isoformat()}"
        )
    if row["closed"] and effective.isoformat() <= row["closed"]:
        raise LedgerError(
            f"{instrument_id} holdings are fixed up to its record date {row['closed']}; "
            f"a movement can't be dated {effective.isoformat()}"
        )


# ---------------------------------------------------------------- reading


def _entries(conn: sqlite3.Connection, as_of: date | None, where: str = "", args: tuple = ()):
    sql = ("SELECT e.owner, e.asset, e.amount FROM ledger_entries e JOIN ledger_txns t ON t.id = e.txn_id"
           " WHERE 1=1" + where)
    if as_of is not None:
        sql += " AND t.effective_date <= ?"
        args = (*args, as_of.isoformat())
    return conn.execute(sql, args)


def balance(conn: sqlite3.Connection, owner: str, asset: str, as_of: date | None = None) -> Decimal:
    """Sum of the owner's lines in this asset, up to and including `as_of` (all time if None)."""
    rows = _entries(conn, as_of, " AND e.owner = ? AND e.asset = ?", (owner, asset))
    return sum((D(r["amount"]) for r in rows), ZERO)  # summed as Decimal, never as SQL floats


def balances(conn: sqlite3.Connection, as_of: date | None = None) -> dict[str, dict[str, Decimal]]:
    """Every non-zero balance: {owner: {asset: amount}}."""
    out: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for r in _entries(conn, as_of):
        out[r["owner"]][r["asset"]] += D(r["amount"])
    return {o: {a: v for a, v in assets.items() if v != 0} for o, assets in out.items()
            if any(v != 0 for v in assets.values())}


def holdings(conn: sqlite3.Connection, asset: str, as_of: date | None = None) -> dict[str, Decimal]:
    """Holders (not issuers) with a non-zero position in `asset`."""
    rows = _entries(conn, as_of, " AND e.asset = ? AND e.owner NOT LIKE ?", (asset, ISSUER_PREFIX + "%"))
    out: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        out[r["owner"]] += D(r["amount"])
    return {k: v for k, v in sorted(out.items()) if v != 0}


def check_integrity(conn: sqlite3.Connection) -> list[str]:
    """Problems found, if any: unbalanced transactions or a party below zero that may not be."""
    problems = []
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for r in conn.execute("SELECT txn_id, asset, amount FROM ledger_entries"):
        totals[(r["txn_id"], r["asset"])] += D(r["amount"])
    problems += [f"{t} does not balance in {a}: {v}" for (t, a), v in totals.items() if v != 0]
    roles = {p["id"]: p["role"] for p in parties(conn)}
    for owner, assets in balances(conn).items():
        if roles.get(owner) not in MAY_GO_NEGATIVE:
            problems += [f"{owner} is below zero in {a}: {v}" for a, v in assets.items() if v < 0]
    return problems


# ---------------------------------------------------------------- transfers


def transfer(conn: sqlite3.Connection, txn_id: str, sender: str, receiver: str, asset: str,
             amount: Decimal, effective_date: date, memo: str = "") -> bool:
    """Move units or cash from one holder to another, free of payment. A sale is a `trade`."""
    if amount <= 0:
        raise LedgerError("a transfer amount must be positive")
    if sender == receiver:
        raise LedgerError("a holder can't transfer to itself")
    _trading_parties(conn, sender, receiver,
                     "transfers are between holders; issuers move units by issuing or redeeming")
    return post(conn, txn_id, "transfer", effective_date,
                [Line(sender, asset, -amount), Line(receiver, asset, amount)], memo)


def trade(conn: sqlite3.Connection, txn_id: str, bond: FixedRateBond, buyer: str, seller: str,
          face: Decimal, clean_price: Decimal, trade_date: date, memo: str = "") -> Settlement:
    """Delivery versus payment: units to the buyer and cash to the seller in one transaction.

    Quotes are clean prices. The buyer pays clean plus accrued interest, except on a bond
    in grace period or default, which trades flat: the price already includes the claim to
    the missed coupon. Settles on the trade date (no T+1 yet). Idempotent by `txn_id`.
    """
    if face <= 0 or clean_price <= 0:
        raise LedgerError("a trade needs a positive face amount and price")
    if buyer == seller:
        raise LedgerError("a party can't trade with itself")
    _trading_parties(conn, buyer, seller, "trades are between holders, the market maker and the market")
    flat = _trades_flat(conn, bond.id)
    accrued = ZERO if flat else accrued_per_100(bond, trade_date)
    cash = money(face * (clean_price + accrued) / HUNDRED)
    posted = post(conn, txn_id, "trade", trade_date, [
        Line(seller, bond.id, -face), Line(buyer, bond.id, face),
        Line(buyer, bond.currency, -cash), Line(seller, bond.currency, cash),
    ], memo)
    return Settlement(txn_id, posted, face, clean_price, accrued, flat, cash)


def _trading_parties(conn: sqlite3.Connection, a: str, b: str, message: str) -> None:
    roles = _roles(conn, {a, b})
    if any(roles.get(p, "holder") not in TRADING_ROLES for p in (a, b)):  # unknown parties: post() names them
        raise LedgerError(message)


def _trades_flat(conn: sqlite3.Connection, instrument_id: str) -> bool:
    """A bond in grace period or default trades without accrued interest (see lifecycle)."""
    row = conn.execute("SELECT status FROM instrument_status WHERE instrument_id = ?", (instrument_id,)).fetchone()
    return row is not None and row["status"] in ("grace_period", "defaulted")


# ---------------------------------------------------------------- seeding


@dataclass(frozen=True)
class HoldersConfig:
    holders: list[dict]
    positions: list[dict]


def load_holders_config(path: Path) -> HoldersConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return HoldersConfig(holders=data["holders"], positions=data["positions"])


def seed(conn: sqlite3.Connection, config: HoldersConfig, instruments: list[Instrument]) -> int:
    """Create the test holders and issue their starting positions. Safe to run every start.

    Returns how many new transactions were posted (0 once seeded).
    """
    by_id = {i.id: i for i in instruments}
    posted = 0
    with atomic(conn):
        record_bond_terms(conn, instruments)
        for h in config.holders:
            add_party(conn, h["id"], h["name"], "holder", h.get("description", ""),
                      bool(h.get("payments_blocked", False)))
        for pos in config.positions:
            inst = by_id.get(pos["instrument"])
            if inst is None:
                raise LedgerError(f"holders config names unknown instrument {pos['instrument']}")
            issued_on = date.fromisoformat(pos["issued_on"])
            if isinstance(inst, FixedRateBond) and issued_on != inst.issue_date:
                raise LedgerError(f"{inst.id} positions must start on its issue date {inst.issue_date}")
            issuer = issuer_id(inst.id)
            add_party(conn, issuer, f"Issuer of {inst.name}", "issuer", "Simulated issuer.")
            units = {holder: D(v) for holder, v in pos["holdings"].items()}
            if any(v <= 0 for v in units.values()):
                raise LedgerError(f"{inst.id} holdings must be positive")
            lines = [Line(issuer, inst.id, -sum(units.values()))]
            lines += [Line(holder, inst.id, v) for holder, v in units.items()]
            posted += post(conn, f"issue:{inst.id}", "issue", issued_on, lines,
                           memo=f"Starting positions in {inst.id} (simulated issuance)")
    return posted


def record_bond_terms(conn: sqlite3.Connection, instruments: list[Instrument]) -> None:
    """Store each bond's final record date, which the ledger enforces (see `_books_open`)."""
    now = datetime.now(timezone.utc).isoformat()
    for bond in (i for i in instruments if isinstance(i, FixedRateBond)):
        final = bond.record_date(bond.maturity_date)  # counted from the scheduled date, before any roll
        conn.execute(
            "INSERT INTO bond_terms(instrument_id, final_record_date, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(instrument_id) DO UPDATE SET final_record_date = excluded.final_record_date,"
            " updated_at = excluded.updated_at WHERE final_record_date != excluded.final_record_date",
            (bond.id, final.isoformat(), now),
        )
