"""Live against paper, fill by fill, on the same quotes (M8 step 7). TESTNET: plumbing only.

Quotes are matched by the paper decision they came from and the side. For each that filled
anywhere, how much did live fill, and how much would each paper rule have said?

Testnet is thin, and our own offers are part of it: a real fill of our live offer is also a
trade the monitor records, and paper then counts it. Those are flagged, as evidence about
the plumbing and not about the paper rules. Testnet prices say nothing about profit.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from debtlab.money import D, ZERO, plain
from debtlab.xrpl_market.live import LABEL


@dataclass
class Row:
    decision_id: int | None
    side: str
    live_maker: Decimal = ZERO
    live_taker: Decimal = ZERO
    conservative: Decimal = ZERO
    optimistic: Decimal = ZERO
    own_trades: int = 0

    @property
    def kind(self) -> str:
        paper = self.conservative > 0 or self.optimistic > 0
        if self.live_taker > 0:
            return "live took liquidity"
        if self.live_maker > 0 and not paper:
            return "live only"
        if self.live_maker == 0 and paper:
            return "paper only"
        if self.live_maker in (self.conservative, self.optimistic):
            return "both"
        return "both, amounts differ"


def compare(live: sqlite3.Connection, paper: sqlite3.Connection) -> list[Row]:
    rows: dict[tuple, Row] = {}
    live_txs = set()

    def row(decision, side) -> Row:
        return rows.setdefault((decision, side), Row(decision, side))
    for f in live.execute("SELECT f.*, o.decision_id FROM live_fills f LEFT JOIN live_orders o ON o.id = f.order_id"):
        r = row(f["decision_id"], f["side"])
        live_txs.add(f["tx_hash"])
        if f["role"] == "maker":
            r.live_maker += D(f["xrp"])
        else:
            r.live_taker += D(f["xrp"])
    for f in paper.execute("SELECT f.*, o.decision_id FROM paper_fills f JOIN paper_orders o ON o.id = f.order_id"):
        r = row(f["decision_id"], f["side"])
        if f["account"] == "conservative":
            r.conservative += D(f["xrp"])
        else:
            r.optimistic += D(f["xrp"])
        r.own_trades += sum(1 for t in json.loads(f["trades"]) if t.get("tx_hash") in live_txs)
    return sorted(rows.values(), key=lambda r: (r.decision_id is None, r.decision_id or 0, r.side))


def fees(live: sqlite3.Connection, paper: sqlite3.Connection) -> dict:
    """Network fees paid or charged, in XRP. Live pays for every refresh; paper is charged only on a requote."""
    paid = sum((-D(r["xrp"]) for r in live.execute("SELECT xrp FROM live_journal WHERE kind = 'fee'")), ZERO)
    out = {"live": paid}
    for a in ("conservative", "optimistic"):
        out[a] = sum((-D(r["xrp"]) for r in paper.execute(
            "SELECT xrp FROM paper_journal WHERE account = ? AND kind = 'fee'", (a,))), ZERO)
    out["live_transactions"] = live.execute("SELECT COUNT(*) FROM live_txs WHERE status IN ('applied', 'failed')").fetchone()[0]
    return out


def render(rows: list[Row], fee: dict, label: str = LABEL) -> str:
    lines = [f"{label}: live against paper, fill by fill", ""]
    if not rows:
        lines.append("No fills yet, live or paper.")
    for r in rows:
        own = f"  [{r.own_trades} paper trade(s) were our own live fill]" if r.own_trades else ""
        lines.append(f"decision {r.decision_id} {r.side:<4}  live {plain(r.live_maker + r.live_taker)}  conservative "
                     f"{plain(r.conservative)}  optimistic {plain(r.optimistic)}  XRP: {r.kind}{own}")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    if counts:
        lines += ["", "Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))]
    lines += ["", f"Network fees, XRP: live {plain(fee['live'])} ({fee['live_transactions']} transactions), conservative "
                  f"{plain(fee['conservative'])}, optimistic {plain(fee['optimistic'])}.",
              "Live pays for an offer about every 15-25 s because offers expire by themselves; paper is charged only when "
              "the price moves. M7's paper fees don't include that.",
              ("Testnet prices aren't real: this shows the plumbing works, and says nothing about profit."
               if label == LABEL else "Real fills on mainnet; paper's are an upper bound: it assumes first place in the queue.")]
    return "\n".join(lines)
