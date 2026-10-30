"""The market maker's P&L, split four ways (M4): spread captured + inventory MTM − fees − slippage.

Everything is per currency and uses dirty values per 100 (clean + accrued), so the
accrued interest paid or received on a fill cancels against the accrued in the marks.
Marks are the bot's own model fair values: there is no market price to mark against.

- **Spread captured**: on each client fill, how much better than its own fair value the
  bot traded, measured on the cash actually booked. Buying: size x fair / 100 - cash paid.
  Selling: cash received - size x fair / 100.
- **Slippage**: the same measure on unwinds against the outside market, as a cost.
- **Inventory MTM**, in two parts (Ethan's change 2): the *market move on the starting
  position*, starting units x (end mark - start mark), which the bot would have had
  without trading at all; and *MTM on traded inventory*, each fill's signed units x
  (end mark - fair at the fill), which is what the trading itself left it exposed to.
- **Fees**: as charged.

The total is worked out separately, from cash and inventory alone, so the identity
total = spread + MTM - fees - slippage is a real check, not true by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from debtlab.money import HUNDRED, ZERO


@dataclass(frozen=True)
class FillForPnl:
    instrument_id: str
    currency: str
    side: str  # the market maker's: buy | sell
    client: str  # uninformed | informed | unwind
    size: Decimal  # face
    cash: Decimal  # as booked
    fair_dirty: Decimal  # the bot's fair value per 100 at the fill
    fee: Decimal


@dataclass(frozen=True)
class PnL:
    currency: str
    spread: Decimal
    start_mtm: Decimal  # market move on the starting position
    traded_mtm: Decimal  # MTM on inventory bought or sold during the run
    fees: Decimal
    slippage: Decimal

    @property
    def mtm(self) -> Decimal:
        return self.start_mtm + self.traded_mtm

    @property
    def total(self) -> Decimal:
        return self.spread + self.mtm - self.fees - self.slippage


def attribute(fills: list[FillForPnl], currencies: dict[str, str], start_units: dict[str, Decimal],
              start_marks: dict[str, Decimal], end_marks: dict[str, Decimal]) -> dict[str, PnL]:
    """The parts per currency. `currencies`, units and marks are keyed by instrument id."""
    parts: dict[str, dict[str, Decimal]] = {
        c: {"spread": ZERO, "start_mtm": ZERO, "traded_mtm": ZERO, "fees": ZERO, "slippage": ZERO}
        for c in set(currencies.values())}
    for inst, units in start_units.items():
        parts[currencies[inst]]["start_mtm"] += units * (end_marks[inst] - start_marks[inst]) / HUNDRED
    for f in fills:
        signed = f.size if f.side == "buy" else -f.size
        value = f.size * f.fair_dirty / HUNDRED
        edge = value - f.cash if f.side == "buy" else f.cash - value
        p = parts[f.currency]
        if f.client == "unwind":
            p["slippage"] -= edge
        else:
            p["spread"] += edge
        p["traded_mtm"] += signed * (end_marks[f.instrument_id] - f.fair_dirty) / HUNDRED
        p["fees"] += f.fee
    return {c: PnL(c, **v) for c, v in parts.items()}


def total(currencies: dict[str, str], start_cash: dict[str, Decimal], end_cash: dict[str, Decimal],
          start_units: dict[str, Decimal], end_units: dict[str, Decimal],
          start_marks: dict[str, Decimal], end_marks: dict[str, Decimal]) -> dict[str, Decimal]:
    """Change in cash (fees included) plus change in inventory value, per currency."""
    out = {c: end_cash.get(c, ZERO) - start_cash.get(c, ZERO) for c in set(currencies.values())}
    for inst, ccy in currencies.items():
        out[ccy] += (end_units.get(inst, ZERO) * end_marks[inst] - start_units.get(inst, ZERO) * start_marks[inst]) / HUNDRED
    return out
