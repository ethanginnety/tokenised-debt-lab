"""The paper risk check (M7 step 6): a `RiskCheck` with the brief's live limits in euro.

Every paper order passes it before it is placed, and it lists every reason it fails, not
just the first. Limits and state come from the paper database each time. It assumes the
worst, as M4 does: every open order on the account fills.

    the bot is running (not paused or killed)
    RLUSD/XRP, with the verified RLUSD issuer only (the look-alike and any other refused)
    the market is usable: data no older than the limit (30 s), monitor connected, book
        valid, a fresh EUR rate
    passive only: a bid below the best offer, an offer above the best bid (decision G)
    XRPL precision: whole drops; the RLUSD amount within 15 significant digits
    at most EUR 10 an order; at most 2 open orders an account; the account's open orders
        together at most EUR 50 (the total)
    the account's loss today under EUR 5 (a breach also pauses the bot)
    funds: a bid's RLUSD, and an offer's XRP, are there after every open order on the
        account fills, keeping back the XRP reserve (base, trust line, one per offer after
        this one) and the fees to place and cancel it
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Mapping

from debtlab.marketmaker.interfaces import Order
from debtlab.money import D
from debtlab.xrpl_market.paper import PAIR, account
from debtlab.xrpl_market.paper.db import limits, state
from debtlab.xrpl_market.paper.market import Market

DROP = Decimal("0.000001")
IOU_DIGITS = 15


@dataclass(frozen=True)
class PaperOrder(Order):
    account: str = ""
    issuer: str = ""


def iou_ok(v: Decimal) -> bool:
    """An issued-currency amount the XRP Ledger can hold exactly: at most 15 significant digits."""
    t = v.normalize().as_tuple()
    return len(t.digits) <= IOU_DIGITS


def committed(others: list[dict]) -> tuple[Decimal, Decimal, int]:
    """RLUSD held by open bids, XRP held by open offers, and how many offers, among `others`."""
    rl = sum((D(o["remaining"]) * D(o["price"]) for o in others if o["side"] == "buy"), Decimal(0))
    xr = sum((D(o["remaining"]) for o in others if o["side"] == "sell"), Decimal(0))
    return rl, xr, len(others)


def others_for(conn: sqlite3.Connection, order: PaperOrder, pending: tuple[Order, ...],
               replacing: frozenset[str]) -> list[dict]:
    out = [o for o in account.open_orders(conn, order.account) if o["id"] != order.id and o["id"] not in replacing]
    out += [{"id": o.id, "side": o.side, "price": o.price, "remaining": o.size} for o in pending
            if o.id != order.id and getattr(o, "account", None) == order.account]
    return out


def held_back(m: Market, cfg: dict, others: int) -> Decimal:
    """The XRP an offer on this account needs left alone: the reserve with one more offer, and fees."""
    return account.reserve(m, cfg, others + 1) + 2 * m.fee_xrp


def affordable(conn: sqlite3.Connection, acct: str, side: str, price: Decimal, m: Market, cfg: dict,
               others: list[dict]) -> Decimal:
    """The largest size (XRP, whole drops) the account could have on this side now."""
    xrp, rlusd = account.balances(conn, acct)
    rl, xr, n = committed(others)
    held = held_back(m, cfg, n)
    if side == "buy":
        # As live: a bid spends RLUSD and only needs what's held back, which a resting sell can't take.
        if xrp < held:
            return Decimal(0)
        size = (rlusd - rl) / price
    else:
        size = xrp - held - xr
    return max(size, Decimal(0)).quantize(DROP, rounding=ROUND_FLOOR)


class PaperRiskCheck:
    name = "PAPER: the brief's live limits in euro, from the paper database"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.issuer = cfg["rlusd"]["issuer"]
        self.lookalikes = set(cfg.get("known_lookalikes", {}))

    def check(self, conn: sqlite3.Connection, order: PaperOrder, marks: Mapping[str, Market], now: datetime,
              pending: tuple[Order, ...] = (), replacing: frozenset[str] = frozenset()) -> list[str]:
        reasons: list[str] = []
        st = state(conn)
        if st["state"] != "running":
            reasons.append(f"bot is {st['state']} ({st['reason']})")
        if order.side not in ("buy", "sell") or order.size <= 0 or order.price <= 0:
            return reasons + [f"malformed order: {order.side} {order.size} at {order.price}"]
        if order.instrument_id != PAIR:
            reasons.append(f"{order.instrument_id} is not {PAIR}")
        if order.issuer != self.issuer:
            what = "a known look-alike" if order.issuer in self.lookalikes else "not the verified RLUSD issuer"
            reasons.append(f"RLUSD issuer {order.issuer or '(none)'} is {what}; only {self.issuer}")
        if not any(a["name"] == order.account for a in account.accounts(conn)):
            return reasons + [f"no open paper account {order.account!r}"]
        m = marks.get(PAIR)
        if m is None:
            return reasons + [f"no market data for {PAIR}"]
        lim = limits(conn)
        for name in ("max_order_eur", "max_daily_loss_eur", "max_total_eur", "max_data_age_seconds", "max_open_orders"):
            if name not in lim:
                reasons.append(f"limit {name} is not set")
        reasons += m.problems(lim.get("max_data_age_seconds", D(30)))
        if order.size != order.size.quantize(DROP):
            reasons.append(f"size {order.size} XRP isn't whole drops")
        if not iou_ok(order.size * order.price):
            reasons.append(f"RLUSD amount {order.size * order.price} has more than {IOU_DIGITS} significant digits")
        if order.side == "buy" and m.best_ask is not None and order.price >= m.best_ask:
            reasons.append(f"bid {order.price} at or above the best offer {m.best_ask}: would take liquidity")
        if order.side == "sell" and m.best_bid is not None and order.price <= m.best_bid:
            reasons.append(f"offer {order.price} at or below the best bid {m.best_bid}: would take liquidity")
        if m.fx is None:
            return reasons
        eur = m.to_eur(order.size * order.price)
        if (cap := lim.get("max_order_eur")) is not None and eur > cap:
            reasons.append(f"order EUR {eur:.4f} over the maximum {cap}")
        others = others_for(conn, order, pending, replacing)
        if (cap := lim.get("max_open_orders")) is not None and len(others) + 1 > cap:
            reasons.append(f"{len(others)} orders already open on the {order.account} account; the maximum is {cap}")
        total = eur + sum((m.to_eur(D(o["remaining"]) * D(o["price"])) for o in others), Decimal(0))
        if (cap := lim.get("max_total_eur")) is not None and total > cap:
            reasons.append(f"open orders would total EUR {total:.4f}; the maximum is {cap}")
        day = account.daily_pnl_eur(conn, order.account, m, now)
        if (cap := lim.get("max_daily_loss_eur")) is not None and day is not None and day <= -cap:
            reasons.append(f"daily loss EUR {-day:.4f} on the {order.account} account has reached the maximum {cap}")
        can = affordable(conn, order.account, order.side, order.price, m, self.cfg, others)
        if order.size > can:
            xrp, _ = account.balances(conn, order.account)
            if order.side == "buy" and xrp < (held := held_back(m, self.cfg, len(others))):
                reasons.append(f"not enough XRP for the reserve and fees on the {order.account} account: {xrp} held, {held} needed")
            else:
                what = "RLUSD" if order.side == "buy" else "XRP"
                reasons.append(f"not enough {what} on the {order.account} account: room for {can} XRP after open orders,"
                               f" the reserve and fees")
        return reasons

    def after_refusal(self, conn: sqlite3.Connection, marks: Mapping[str, Market], now: datetime) -> bool:
        from debtlab.xrpl_market.paper import controls
        return controls.enforce_daily_loss(conn, marks.get(PAIR), now, self.cfg) is not None
