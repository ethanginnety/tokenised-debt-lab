"""The market maker's starting book: its parties, units and cash.

Three parties join the ledger. The market maker quotes from its own account; the
simulated market is the other side of every client order; the funding account
brings the starting cash in from outside the simulation (the only party besides
issuers allowed a negative balance, and in cash only). Units come from a simulated
tap issue of each bond on its issue date, so the test holders' positions are
untouched. Both trading parties hold units and cash and can never go below zero,
so neither can sell short.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from debtlab import ledger
from debtlab.audit.db import atomic
from debtlab.instruments import Instrument
from debtlab.ledger import LedgerError, Line
from debtlab.money import D
from debtlab.pricing.bond import FixedRateBond

ROLES = ("market_maker", "market", "funding")


@dataclass(frozen=True)
class BookConfig:
    parties: dict[str, dict]  # role -> {id, name, description}
    funded_on: date
    units: dict[str, dict[str, str]]  # instrument id -> {role: face}
    cash: dict[str, dict[str, str]]  # currency -> {role: amount}

    def party(self, role: str) -> str:
        return self.parties[role]["id"]


def load_book_config(path: Path) -> BookConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return BookConfig(data["parties"], date.fromisoformat(data["funded_on"]), data["units"], data["cash"])


def seed_book(conn: sqlite3.Connection, config: BookConfig, instruments: list[Instrument]) -> int:
    """Create the parties and post the starting book. Safe to run every start.

    Returns how many new transactions were posted (0 once seeded). Raises LedgerError,
    and writes nothing, if the books are already closed (a scenario database whose
    payments were scheduled before the market maker existed): recreate it.
    """
    by_id = {i.id: i for i in instruments}
    posted = 0
    with atomic(conn):
        for role in ROLES:
            p = config.parties[role]
            ledger.add_party(conn, p["id"], p["name"], role, p.get("description", ""))
        for inst_id, by_role in config.units.items():
            bond = by_id.get(inst_id)
            if not isinstance(bond, FixedRateBond):
                raise LedgerError(f"market maker config names {inst_id}, which is not a configured bond")
            units = _amounts(by_role, inst_id)
            issuer = ledger.issuer_id(bond.id)
            ledger.add_party(conn, issuer, f"Issuer of {bond.name}", "issuer", "Simulated issuer.")
            lines = [Line(issuer, bond.id, -sum(units.values()))]
            lines += [Line(config.party(role), bond.id, v) for role, v in units.items()]
            posted += ledger.post(conn, f"issue:{bond.id}:market-making", "issue", bond.issue_date, lines,
                                  memo=f"Market-making units in {bond.id} (simulated tap issue)")
        for currency, by_role in config.cash.items():
            amounts = _amounts(by_role, currency)
            lines = [Line(config.party("funding"), currency, -sum(amounts.values()))]
            lines += [Line(config.party(role), currency, v) for role, v in amounts.items()]
            posted += ledger.post(conn, f"fund:{currency}", "funding", config.funded_on, lines,
                                  memo=f"Starting {currency} cash for the market maker and the simulated market")
    return posted


def _amounts(by_role: dict[str, str], what: str) -> dict[str, Decimal]:
    amounts = {role: D(v) for role, v in by_role.items()}
    if set(amounts) - {"market_maker", "market"} or any(v <= 0 for v in amounts.values()):
        raise LedgerError(f"{what}: starting amounts go to market_maker and market, and must be positive")
    return amounts
