"""The paper bot's loop (M7 step 6). PAPER: no key, no wallet, no order on any ledger.

One tick, every second or so, in one transaction on the paper database:

    1. read the market as of now from the monitor's database (read-only)
    2. fills: real trades in ledgers up to one before the latest snapshot (the monitor
       records a ledger's trades before its snapshot; the one-ledger lag makes sure none
       is missed), against orders placed in an earlier ledger, under each account's rule
    3. safety: data older than the limit, the monitor down or an outage open, the book
       invalid or no fresh EUR rate -> cancel every open order (the live rule)
    4. a daily-loss breach on either account pauses the bot
    5. on each new ledger: open the accounts if needed (only when running), decide (the
       strategy records it), and requote: a side is replaced in both accounts when its
       price moves by `requote_bp` or more, or when no account has an order on that side

Both accounts get the same prices (decision E); each account's size is the quote's,
cut to what it can afford. Orders left open by a previous run are cancelled at start, and
trades from before the start are never used.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from debtlab.money import D, plain
from debtlab.xrpl_market.paper import LABEL, PAIR, RULES, account, controls, fills
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.paper import market as mk
from debtlab.xrpl_market.paper.risk import PaperOrder, PaperRiskCheck, affordable, others_for
from debtlab.xrpl_market.paper.strategy import BP, PaperStrategy, Quote
from debtlab.xrpl_market.paper.venue import PaperVenue, cancel, cancel_all, place, venues
from debtlab.xrpl_market.view import open_readonly


class PaperBot:
    def __init__(self, conn: sqlite3.Connection, monitor_path: Path | str, cfg: dict,
                 out: Callable[[str], None] = print):
        if not Path(monitor_path).exists():
            raise FileNotFoundError(f"no monitor database at {monitor_path}: start the monitor first")
        self.conn, self.cfg, self.out = conn, cfg, out
        self.mon = open_readonly(Path(monitor_path))
        self.strategy, self.risk = PaperStrategy(cfg), PaperRiskCheck(cfg)
        self.refusals: dict = {}
        self.last_decided: int | None = None
        self.run_id: int | None = None
        self.max_age = D(cfg["paper"]["limits"]["max_data_age_seconds"])

    # --- reading the monitor -----------------------------------------------------------

    def _read(self, now: datetime) -> mk.Market:
        return mk.read_market(self.mon, now, self.cfg)

    def _snapshot(self):
        """One read transaction on the monitor's database for the whole tick."""
        bot = self

        class _Snap:
            def __enter__(self):
                bot.mon.execute("BEGIN")

            def __exit__(self, *_):
                bot.mon.execute("ROLLBACK")
        return _Snap()

    # --- start, tick, stop -------------------------------------------------------------

    def start(self, now: datetime) -> int:
        pdb.seed_limits(self.conn, self.cfg, now)
        self.run_id, gap = pdb.start_run(self.conn, now, float(self.max_age))
        with self._snapshot():
            m = self._read(now)
            with pdb.tx(self.conn):
                left = account.open_orders(self.conn)
                cur = self._cursor()
                start_at = max(cur or 0, (m.ledger_index or 1) - 1)
                if left:   # in force for no trade the bot will read: those start after start_at
                    cancel_all(self.conn, "restart: orders left open by the previous run", now, venues(m), start_at)
                self.conn.execute("INSERT INTO paper_cursor(id, ledger_index) VALUES (1, ?) ON CONFLICT(id) DO UPDATE"
                                  " SET ledger_index = excluded.ledger_index", (start_at,))
                pdb.event(self.conn, now, "run_started", {"run": self.run_id, "cancelled_from_before": len(left),
                                                          "seconds_since_last_alive": gap, "trades_from_ledger": start_at + 1})
        return self.run_id

    def stop(self, now: datetime, reason: str) -> None:
        with self._snapshot():
            m = self._read(now)
            with pdb.tx(self.conn):
                cancel_all(self.conn, f"paper bot stopped: {reason}", now, venues(m))
                pdb.event(self.conn, now, "run_stopped", {"run": self.run_id, "reason": reason})
        pdb.stop_run(self.conn, self.run_id, now, reason)

    def tick(self, now: datetime) -> dict:
        with self._snapshot():
            m = self._read(now)
            vs = venues(m)
            out = {"ledger": m.ledger_index, "fills": 0, "cancelled": 0, "decision": None}
            with pdb.tx(self.conn):
                pdb.heartbeat(self.conn, self.run_id, now)
                out["fills"] = self._fills(m, now, vs)
                opened = bool(account.accounts(self.conn))
                if opened and m.mid is not None and m.valid:
                    account.record_day_open(self.conn, m, now)
                problems = m.problems(self.max_age)
                if problems and account.open_orders(self.conn):
                    out["cancelled"] += cancel_all(self.conn, "safety: " + "; ".join(problems), now, vs)
                controls.enforce_daily_loss(self.conn, m, now, self.cfg)
                if m.ledger_index is not None and m.ledger_index != self.last_decided:
                    self.last_decided = m.ledger_index
                    if not opened and not problems and pdb.state(self.conn)["state"] == "running":
                        account.open_accounts(self.conn, m, self.cfg, now)
                        account.record_day_open(self.conn, m, now)
                    (q,) = self.strategy.decide(self.conn, m, now)
                    out["decision"] = q
                    if not q.quoted:
                        if account.open_orders(self.conn):
                            out["cancelled"] += cancel_all(self.conn, "no quote: " + "; ".join(q.reasons), now, vs)
                    else:
                        out["cancelled"] += self._requote(q, m, now, vs)
        return out

    # --- fills ---------------------------------------------------------------------------

    def _cursor(self) -> int | None:
        r = self.conn.execute("SELECT ledger_index FROM paper_cursor WHERE id = 1").fetchone()
        return None if r is None else r["ledger_index"]

    def _fills(self, m: mk.Market, now: datetime, vs: dict[str, PaperVenue]) -> int:
        cur = self._cursor()
        if m.ledger_index is None or cur is None:
            return 0
        upto = m.ledger_index - 1
        if upto <= cur:
            return 0
        by_ledger: dict[int, list[fills.Trade]] = defaultdict(list)
        for r in mk.trades(self.mon, cur, upto, now):
            by_ledger[r["ledger_index"]].append(fills.Trade(r["ledger_index"], r["tx_hash"], r["node"], r["venue"],
                                                            r["taker_side"], D(r["price"]), D(r["xrp"])))
        rules = {a["name"]: a["rule"] for a in account.accounts(self.conn)}
        n = 0
        for ledger in sorted(by_ledger):
            for o in account.in_force(self.conn, ledger):
                if o["account"] not in rules:
                    continue
                qty, used = fills.fill(o["side"], D(o["price"]), o["remaining"], by_ledger[ledger], rules[o["account"]])
                funded = self._funded(o, m)
                if qty > funded:      # the ledger fills an offer only as far as the account can pay
                    used.append({"capped_by_funds": plain(funded), "would_have_filled": plain(qty)})
                    qty = funded
                if qty > 0:
                    mid = mk.mid_at_ledger(self.mon, ledger) or m.mid
                    self._book_fill(o, ledger, qty, used, mid, now)
                    n += 1
        self.conn.execute("UPDATE paper_cursor SET ledger_index = ? WHERE id = 1", (upto,))
        return n

    def _funded(self, o: dict, m: mk.Market) -> Decimal:
        xrp, rlusd = account.balances(self.conn, o["account"])
        if o["side"] == "buy":
            room = rlusd / D(o["price"])
        else:
            room = xrp - account.reserve(m, self.cfg, len(account.open_orders(self.conn, o["account"])))
        return max(room, Decimal(0)).quantize(fills.DROP, rounding="ROUND_DOWN")

    def _book_fill(self, o: dict, ledger: int, qty: Decimal, used: list[dict], mid: Decimal, now: datetime) -> None:
        price = D(o["price"])
        rl = qty * price
        dx, dr = (qty, -rl) if o["side"] == "buy" else (-qty, rl)
        fid = f"{o['id']}:{ledger}"
        account.journal(self.conn, o["account"], now, ledger, "fill", dx, dr, mid, fid,
                        f"{o['side']} {plain(qty)} XRP at {plain(price)} ({o['account']} rule)")
        self.conn.execute("INSERT INTO paper_fills(id, account, order_id, ledger_index, at, side, price, xrp, rlusd, mid,"
                          " trades) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (fid, o["account"], o["id"], ledger, pdb.iso(now), o["side"], plain(price), plain(qty),
                           plain(rl), plain(mid), json.dumps(used)))
        filled = D(o["filled"]) + qty
        done = filled >= D(o["size"])
        if o["status"] == "open":
            self.conn.execute("UPDATE paper_orders SET filled = ?, status = ?, closed_at = ?, closed_ledger = ?"
                              " WHERE id = ?", (plain(filled), "filled" if done else "open",
                                               pdb.iso(now) if done else None, ledger if done else None, o["id"]))
        else:   # cancelled after this ledger's trades: it filled before the cancel could land
            self.conn.execute("UPDATE paper_orders SET filled = ?, status = ? WHERE id = ?",
                              (plain(filled), "filled" if done else "cancelled", o["id"]))
        pdb.event(self.conn, now, "paper_fill", {"id": fid, "account": o["account"], "side": o["side"],
                                                 "price": plain(price), "xrp": plain(qty), "mid": plain(mid),
                                                 "trades": len(used), "label": LABEL})

    # --- quoting -------------------------------------------------------------------------

    def _requote(self, q: Quote, m: mk.Market, now: datetime, vs: dict[str, PaperVenue]) -> int:
        s = self.cfg["paper"]["strategy"]
        cancelled = 0
        for side, price in (("buy", q.bid), ("sell", q.ask)):
            cur = [o for o in account.open_orders(self.conn) if o["side"] == side]
            if cur:
                old = D(cur[0]["price"])
                if abs(price - old) / q.fair * BP < D(s["requote_bp"]):
                    continue
                cancelled += cancel(self.conn, cur, f"requote: {plain(old)} -> {plain(price)}", now, vs)
            for acct in RULES:
                probe = PaperOrder(f"q{q.id}:{side}:{acct}", PAIR, side, price, q.bid_size, account=acct,
                                   issuer=self.cfg["rlusd"]["issuer"])
                room = affordable(self.conn, acct, side, price, m, self.cfg, others_for(self.conn, probe, (), frozenset()))
                size = min(q.bid_size if side == "buy" else q.ask_size, room)
                if size < D(s["min_order_xrp"]):
                    size = q.bid_size if side == "buy" else q.ask_size   # let the risk check say why
                order = PaperOrder(probe.id, PAIR, side, price, size, account=acct, issuer=probe.issuer)
                place(self.conn, order, m, now, self.risk, vs[acct], q.id, self.refusals)
        return cancelled

    # --- running -------------------------------------------------------------------------

    def run(self, until: datetime | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            sleep: Callable[[float], None] = time.sleep) -> str:
        tick_s = float(self.cfg["paper"]["tick_seconds"])
        self.start(clock())
        self.out(f"{LABEL}: paper bot started (run {self.run_id}); state {pdb.state(self.conn)['state']}. "
                 "Nothing here places an order on any ledger.")
        reason = "finished (--for)"
        try:
            while until is None or clock() < until:
                r = self.tick(clock())
                if r["fills"]:
                    self.out(f"{LABEL}: {r['fills']} paper fill(s) at ledger {r['ledger']}")
                if r["ledger"] and r["ledger"] % 50 == 0 and r["decision"] is not None:
                    q = r["decision"]
                    self.out(f"{LABEL}: ledger {r['ledger']} " + (f"bid {q.bid} / ask {q.ask}" if q.quoted
                                                                  else "no quote: " + "; ".join(q.reasons)))
                sleep(tick_s)
        except KeyboardInterrupt:
            reason = "stopped by user"
        self.stop(clock(), reason)
        self.out(f"{LABEL}: paper bot stopped ({reason}); open orders cancelled.")
        return reason
