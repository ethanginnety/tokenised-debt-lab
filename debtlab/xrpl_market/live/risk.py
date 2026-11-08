"""The live risk check (M8 step 6): the paper check's limits, with balances from the validated ledger.

Every offer passes it before it is signed, and it lists every reason it fails. Limits and
state come from the live database each time. It assumes the worst: every open order fills.

    the bot is running; the verified pair and testnet RLUSD issuer only
    the market is usable: data no older than the limit, monitor connected, book valid, fresh rate
    the close-time budget holds (decision B)
    never intends to take (decision C): a bid at least `taker_margin_bp` below the best offer
        AND what the AMM charges with its fee, an offer that far above the best bid and what the
        AMM pays with its fee, and neither crossing our own other offer. An unknown AMM price is
        a refusal. The monitor must be leaving this account's offers out of the prices
    whole drops; the RLUSD amount within 15 significant digits
    at most EUR 10 an order, at most 2 open orders, and capital in plus open orders within EUR 50
    the loss today under EUR 5
    funds on the ledger, within the budget, after every open order fills, keeping the reserve and fees;
        a resting offer can't take XRP below the reserve, so its size never blocks a bid

An offer being replaced counts in full against the budget and the EUR 50 total until the replacement
is settled (mainnet session 1, 7 Nov 2026). The replacement is sized from the last validated ledger;
a taker can fill the old offer after that, even in the ledger the replacement applies, ahead of it,
and then the replacement's cancel finds nothing and it goes up whole. Session 1 sold one quote's worth
twice that way, and once went 1.88 XRP over its budget. The two-order limit still leaves the old offer
out: the replacement removes it in the same transaction or finds it gone, so the ledger never holds
both (checked on session 1's 889 transactions).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Mapping, NamedTuple

from debtlab.marketmaker.interfaces import Order
from debtlab.money import D, ZERO
from debtlab.xrpl_market import closetimes
from debtlab.xrpl_market.book import amm_quotes
from debtlab.xrpl_market.live import book
from debtlab.xrpl_market.live.db import limits, state
from debtlab.xrpl_market.live.ledger import AccountState, Header
from debtlab.xrpl_market.paper import PAIR
from debtlab.xrpl_market.paper.market import Market
from debtlab.xrpl_market.paper.risk import IOU_DIGITS, committed, iou_ok
from debtlab.xrpl_market.store import iso

BP = Decimal(10_000)
DROP = Decimal("0.000001")
SELL_LEAVES_ROOM_FOR_OBJECTS = 2   # expired offers awaiting removal raise the reserve; a sell using all spare XRP would refuse the next buy


class AmmQuotes(NamedTuple):
    bid: Decimal      # what the pool pays for XRP, its fee taken off
    ask: Decimal      # what the pool charges for XRP, its fee added


@dataclass(frozen=True)
class LiveMark:
    market: Market
    header: Header
    account: AccountState
    amm: AmmQuotes | None
    currency: str = "RLUSD"


def amm_prices(mon: sqlite3.Connection, now: datetime) -> AmmQuotes | None:
    """What the AMM would pay and charge, in the monitor's latest snapshot."""
    r = mon.execute("SELECT amm_xrp, amm_rlusd, amm_fee_pct FROM xrpl_snapshots WHERE received_at <= ?"
                    " ORDER BY ledger_index DESC LIMIT 1", (iso(now),)).fetchone()
    if r is None or not r["amm_xrp"] or not r["amm_rlusd"] or r["amm_fee_pct"] is None or D(r["amm_xrp"]) <= 0:
        return None
    return AmmQuotes(*amm_quotes(D(r["amm_rlusd"]) / D(r["amm_xrp"]), D(r["amm_fee_pct"])))


def own_offers_problem(m: Market, address: str) -> str | None:
    """The bot must never price from its own quotes, so the monitor has to be leaving them out."""
    if m.ledger_index is None or m.excluded_account == address:
        return None
    return f"the monitor isn't leaving out {address}'s offers: start it with --exclude-account {address}"


def others_for(conn: sqlite3.Connection, order_id: str, replacing: frozenset[str]) -> list[dict]:
    """Open orders that still count: not this one, not one being replaced now or by a transaction under way."""
    gone = {r["replaces"] for r in conn.execute("SELECT replaces FROM live_orders WHERE status = 'pending' AND replaces IS NOT NULL")}
    return [o for o in book.open_orders(conn) if o["id"] != order_id and o["id"] not in replacing and o["sequence"] not in gone]


def replaced_for(conn: sqlite3.Connection, order_id: str, replacing: frozenset[str]) -> list[dict]:
    """Open orders this one replaces, or a transaction under way replaces. Until that's settled each can
    still fill in full, so they count against the budget and the total, not against the order count."""
    gone = {r["replaces"] for r in conn.execute("SELECT replaces FROM live_orders WHERE status = 'pending' AND replaces IS NOT NULL")}
    return [o for o in book.open_orders(conn) if o["id"] != order_id and (o["id"] in replacing or o["sequence"] in gone)]


def held_back(st: AccountState, m: Market, cfg: dict, others: list[dict]) -> Decimal:
    """The XRP an offer on this account needs left alone: the reserve with one more offer, and fees."""
    # The ledger's own count, if higher: expired offers hold reserve until they're removed.
    keep = m.reserve_base_xrp + m.reserve_inc_xrp * max(cfg["paper"]["trust_lines"] + len(others) + 1, st.owner_count)
    return keep + 2 * m.fee_xrp


def affordable(side: str, price: Decimal, st: AccountState, m: Market, cfg: dict, others: list[dict],
               replaced: list[dict] = ()) -> Decimal:
    """The largest size (XRP, whole drops) the ledger balances allow on this side now. `replaced`: offers
    being replaced, which may yet fill in full, so they hold funds; they hold no reserve, being removed."""
    rl, xr, _ = committed(list(others) + list(replaced))
    held = held_back(st, m, cfg, others)
    if side == "buy":
        # A bid spends RLUSD. Of XRP it only needs what's held back, and a resting sell can't take that: the ledger
        # stops an offer at the reserve. So a sell's size doesn't count here, even one left too big by a fill.
        if st.xrp < held:
            return ZERO
        size = (st.rlusd - rl) / price
    else:
        size = st.xrp - held - xr - m.reserve_inc_xrp * SELL_LEAVES_ROOM_FOR_OBJECTS
    return max(size, ZERO).quantize(DROP, rounding=ROUND_FLOOR)


class LiveRiskCheck:
    name = "TESTNET: the brief's live limits in euro, balances from the validated ledger"

    def __init__(self, cfg: dict):
        self.cfg, self.issuer = cfg, cfg["rlusd"]["issuer"]
        self.lookalikes = set(cfg.get("known_lookalikes", {}))
        self.margin = D(cfg["live"]["taker_margin_bp"])

    def check(self, conn: sqlite3.Connection, order: Order, marks: Mapping[str, LiveMark], now: datetime,
              pending: tuple[Order, ...] = (), replacing: frozenset[str] = frozenset()) -> list[str]:
        reasons: list[str] = []
        st = state(conn)
        if st["state"] != "running":
            reasons.append(f"bot is {st['state']} ({st['reason']})")
        if order.side not in ("buy", "sell") or order.size <= 0 or order.price <= 0:
            return reasons + [f"malformed order: {order.side} {order.size} at {order.price}"]
        if order.instrument_id != PAIR:
            reasons.append(f"{order.instrument_id} is not {PAIR}")
        issuer = getattr(order, "issuer", "")
        if issuer != self.issuer:
            what = "a refused issuer" if issuer in self.lookalikes else "not the verified testnet RLUSD issuer"
            reasons.append(f"RLUSD issuer {issuer or '(none)'} is {what}; only {self.issuer}")
        if book.account(conn) is None:
            return reasons + ["the account isn't open"]
        mark = marks.get(PAIR)
        if mark is None:
            return reasons + [f"no market data for {PAIR}"]
        m = mark.market
        lim = limits(conn)
        for name in ("max_order_eur", "max_daily_loss_eur", "max_total_eur", "max_data_age_seconds", "max_open_orders"):
            if name not in lim:
                reasons.append(f"limit {name} is not set")
        reasons += m.problems(lim.get("max_data_age_seconds", D(30)))
        h = mark.header
        if not closetimes.budget_holds(h.resolution, h.close_flags, self.cfg["offer_expiry"]):
            reasons.append(f"close-time budget doesn't hold (resolution {h.resolution} s, flags {h.close_flags})")
        if order.size != order.size.quantize(DROP):
            reasons.append(f"size {order.size} XRP isn't whole drops")
        if not iou_ok(order.size * order.price):
            reasons.append(f"RLUSD amount {order.size * order.price} has more than {IOU_DIGITS} significant digits")
        if (why := own_offers_problem(m, mark.account.address)):
            reasons.append(why)
        reasons += self._never_take(order, m, mark.amm)
        others = others_for(conn, order.id, replacing)
        replaced = replaced_for(conn, order.id, replacing)
        for o in others:
            if o["side"] != order.side and ((order.side == "buy" and order.price >= D(o["price"])) or
                                            (order.side == "sell" and order.price <= D(o["price"]))):
                reasons.append(f"{order.side} at {order.price} would cross our own offer at {o['price']}")
        if m.fx is None or m.mid is None:
            return reasons
        eur = m.to_eur(order.size * order.price)
        if (cap := lim.get("max_order_eur")) is not None and eur > cap:
            reasons.append(f"order EUR {eur:.4f} over the maximum {cap}")
        if (cap := lim.get("max_open_orders")) is not None and len(others) + 1 > cap:
            reasons.append(f"{len(others)} orders already open; the maximum is {cap}")
        if (cap := lim.get("max_total_eur")) is not None:
            total = eur + sum((m.to_eur(D(o["remaining"]) * D(o["price"])) for o in others + replaced), ZERO)
            if total > cap:
                reasons.append(f"open orders would total EUR {total:.4f}; the maximum is {cap}")
            capital = book.capital_eur(conn)
            if capital > cap:
                reasons.append(f"capital in the account is EUR {capital:.4f}; the maximum is {cap}")
        day = book.daily_pnl_rlusd(conn, m.mid, now)
        if (cap := lim.get("max_daily_loss_eur")) is not None and day is not None and m.to_eur(day) <= -cap:
            reasons.append(f"daily loss EUR {-m.to_eur(day):.4f} has reached the maximum {cap}")
        st = book.within_budget(conn, mark.account)
        can = affordable(order.side, order.price, st, m, self.cfg, others, replaced)
        if order.size > can:
            if order.side == "buy" and st.xrp < (held := held_back(st, m, self.cfg, others)):
                reasons.append(f"not enough XRP for the reserve and fees on the ledger: {st.xrp} held, {held} needed")
            else:
                what = "RLUSD" if order.side == "buy" else "XRP"
                reasons.append(f"not enough {what} on the ledger: room for {can} XRP after open orders, the reserve and fees")
        return reasons

    def _never_take(self, order: Order, m: Market, amm: AmmQuotes | None) -> list[str]:
        out = []
        if amm is None:
            out.append("the AMM's price is unknown, so the margin against taking can't be checked")
        room = self.margin / BP
        if order.side == "buy":
            for what, p in (("best offer", m.best_ask), ("AMM's price with its fee", amm.ask if amm else None)):
                if p is not None and order.price > p * (1 - room):
                    out.append(f"bid {order.price} within {self.margin} bp of the {what} {p}: it could take liquidity")
        else:
            for what, p in (("best bid", m.best_bid), ("AMM's price with its fee", amm.bid if amm else None)):
                if p is not None and order.price < p * (1 + room):
                    out.append(f"offer {order.price} within {self.margin} bp of the {what} {p}: it could take liquidity")
        return out

    def after_refusal(self, conn: sqlite3.Connection, marks: Mapping[str, LiveMark], now: datetime) -> bool:
        return False        # the bot's own tick enforces the daily loss on every ledger
