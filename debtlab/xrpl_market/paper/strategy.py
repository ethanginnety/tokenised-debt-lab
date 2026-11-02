"""The RLUSD/XRP paper strategy (M7 step 5): a `Strategy`, separate from the bond quote model.

Fair value is the mid of the funded best bid and ask in the latest validated ledger read.
No bond value is ever used (brief §7). The half-spread, in bp of the mid, is the sum of:

    base        a fixed minimum (config)
    volatility  realised over the last 15 minutes: the standard deviation of the
                ledger-to-ledger change in mid, scaled to the expected holding time
                (sqrt of `vol_horizon_ledgers`, about a minute) times `vol_mult`
    data age    `age_bp_per_second` times the age of the snapshot
    fees        the network fee to place and cancel, as bp of the order
    inventory   a skew, not a widening: with more than half the driver account's value
                in XRP both prices move down (more eager to sell, less to buy), up to
                `inventory_skew_bp` when it is all XRP; each side's half-spread stays at
                least `min_half_spread_bp`

Prices round away from the mid (bids down, offers up) to `price_decimals`; sizes round
down to whole drops. Size is `order_eur` of XRP at the mid. Both sides stay passive: a
bid at or above the best offer, or an offer at or below the best bid, is never quoted.

No quote at all when: data older than the limit, the monitor down or an outage open, the
book invalid, no fresh EUR rate, too little history for volatility, a daily loss limit
reached, or the bot paused or killed. Every decision is recorded with its reasons.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from debtlab.money import D, plain
from debtlab.xrpl_market.paper import account
from debtlab.xrpl_market.paper.db import iso, limits, state
from debtlab.xrpl_market.paper.market import Market, realised_vol_bp

BP = Decimal(10_000)
DROP = Decimal("0.000001")


@dataclass(frozen=True)
class Quote:
    quoted: bool
    reasons: tuple[str, ...]
    fair: Decimal | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    components: dict = field(default_factory=dict)
    ledger_index: int | None = None
    id: int | None = None


def round_price(p: Decimal, decimals: int, up: bool) -> Decimal:
    return p.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_CEILING if up else ROUND_FLOOR)


class PaperStrategy:
    name = "RLUSD/XRP paper: mid of the live book, half-spread in parts"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.s = cfg["paper"]["strategy"]

    def decide(self, conn: sqlite3.Connection, market: Market, now: datetime) -> list[Quote]:
        q = self.quote(conn, market, now)
        cur = conn.execute(
            "INSERT INTO paper_decisions(at, ledger_index, quoted, reasons, fair, components, bid_price, ask_price,"
            " bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iso(now), market.ledger_index, int(q.quoted), json.dumps(list(q.reasons)), _s(q.fair),
             json.dumps(q.components, sort_keys=True), _s(q.bid), _s(q.ask), _s(q.bid_size), _s(q.ask_size)))
        return [Quote(**{**q.__dict__, "id": cur.lastrowid})]

    def quote(self, conn: sqlite3.Connection, m: Market, now: datetime) -> Quote:
        s, lim = self.s, limits(conn)
        reasons = m.problems(lim.get("max_data_age_seconds", D(30)))
        st = state(conn)
        if st["state"] != "running":
            reasons.append(f"bot is {st['state']} ({st['reason']})")
        for a in account.accounts(conn):
            day = account.daily_pnl_eur(conn, a["name"], m, now)
            cap = lim.get("max_daily_loss_eur")
            if day is not None and cap is not None and day <= -cap:
                reasons.append(f"{a['name']} account: daily loss EUR {-day:.4f} has reached the maximum {cap}")
        sigma, n = realised_vol_bp(m.mids)
        if n < s["vol_min_samples"]:
            reasons.append(f"only {n} mid changes in the last {s['vol_window_minutes']} min; need {s['vol_min_samples']}")
        if m.mid is None or m.fx is None or not m.valid:
            return Quote(False, tuple(reasons), m.mid, ledger_index=m.ledger_index)

        fair = m.mid
        size = (m.from_eur(D(s["order_eur"])) / fair).quantize(DROP, rounding=ROUND_FLOOR)
        base = D(s["base_half_spread_bp"])
        vol = D(s["vol_mult"]) * (sigma or D(0)) * D(s["vol_horizon_ledgers"]).sqrt()
        age = D(s["age_bp_per_second"]) * D(f"{max(m.age_s or 0, 0):.3f}")
        fee = 2 * m.fee_xrp / size * BP if size > 0 else D(0)
        half = base + vol + age + fee
        skew, share = D(0), None
        driver = self.cfg["paper"]["driver_account"]
        if any(a["name"] == driver for a in account.accounts(conn)):
            xrp, rlusd = account.balances(conn, driver)
            total = account.value(xrp, rlusd, fair)
            if total > 0:
                share = xrp * fair / total
                cap = D(s["inventory_skew_bp"])
                skew = max(-cap, min(cap, cap * (share - D("0.5")) / D("0.5")))
        floor = D(s["min_half_spread_bp"])
        bid_half, ask_half = max(half + skew, floor), max(half - skew, floor)
        dp = s["price_decimals"]
        bid = round_price(fair * (1 - bid_half / BP), dp, up=False)
        ask = round_price(fair * (1 + ask_half / BP), dp, up=True)
        comp = {"base_bp": base, "vol_bp": vol, "sigma_ledger_bp": sigma, "vol_samples": n, "age_bp": age,
                "age_s": m.age_s, "fee_bp": fee, "half_spread_bp": half, "inventory_skew_bp": skew,
                "driver_xrp_share": share, "bid_half_bp": bid_half, "ask_half_bp": ask_half,
                "best_bid": m.best_bid, "best_ask": m.best_ask, "usd_per_eur": m.fx.usd_per_eur}
        comp = {k: (plain(v) if isinstance(v, Decimal) else v) for k, v in comp.items()}
        if size < D(s["min_order_xrp"]):
            reasons.append(f"size {size} XRP under the minimum {s['min_order_xrp']}")
        if m.best_ask is not None and bid >= m.best_ask:
            reasons.append(f"bid {bid} would take liquidity (best offer {m.best_ask}); passive only")
        if m.best_bid is not None and ask <= m.best_bid:
            reasons.append(f"offer {ask} would take liquidity (best bid {m.best_bid}); passive only")
        return Quote(not reasons, tuple(reasons), fair, bid, ask, size, size, comp, m.ledger_index)


def _s(x: Decimal | None) -> str | None:
    return None if x is None else plain(x)
