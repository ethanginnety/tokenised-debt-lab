"""The two paper fill rules of decision E. Pure functions.

A paper order isn't on the real book, so whether it would have filled is judged from the
real trades on the pair (book and AMM, decision B) in ledgers after it was placed.

    Our bid at P (we buy XRP) is hit by takers selling XRP. A taker who sold at a price
    *below* P got a worse price than ours, so with our bid on the book they'd have hit us
    first. Conservative: only trades strictly below P count (at P we'd be behind the queue).
    Optimistic: trades at P count too.
    Our offer at P (we sell XRP) mirrors it: takers buying XRP above P (conservative), or at
    or above P (optimistic).

The fill is at most the XRP those trades moved, and at most what is left of the order,
rounded down to whole drops. It is at our price: a resting order trades at its own price.
Limits either way: no market impact, nobody reacts to a paper order, no latency.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

DROP = Decimal("0.000001")


@dataclass(frozen=True)
class Trade:
    ledger_index: int
    tx_hash: str
    node: str
    venue: str          # book | amm
    taker_side: str     # buy_xrp | sell_xrp
    price: Decimal
    xrp: Decimal


def qualifies(side: str, price: Decimal, t: Trade, rule: str) -> bool:
    if rule not in ("conservative", "optimistic"):
        raise ValueError(f"unknown fill rule {rule}")
    if side == "buy":
        return t.taker_side == "sell_xrp" and (t.price < price or (rule == "optimistic" and t.price == price))
    if side == "sell":
        return t.taker_side == "buy_xrp" and (t.price > price or (rule == "optimistic" and t.price == price))
    raise ValueError(f"unknown side {side}")


def fill(side: str, price: Decimal, remaining: Decimal, trades: list[Trade], rule: str) -> tuple[Decimal, list[dict]]:
    """How much of the order fills on these trades (one ledger's), and which trades did it."""
    used, total = [], Decimal(0)
    for t in trades:
        if total >= remaining:
            break
        if qualifies(side, price, t, rule):
            take = min(t.xrp, remaining - total)
            total += take
            used.append({"tx_hash": t.tx_hash, "node": t.node, "venue": t.venue, "price": str(t.price),
                         "xrp": str(take)})
    qty = total.quantize(DROP, rounding=ROUND_DOWN)
    return (qty, used) if qty > 0 else (Decimal(0), [])
