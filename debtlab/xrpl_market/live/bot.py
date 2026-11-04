"""The testnet bot's loop (M8 step 6). TESTNET: test funds, never mainnet.

Each tick, about once a second:

    read the market from the testnet monitor's database (read-only)
    on a new validated ledger: bring the books up to it and check them against it
    stop (cancel everything and pause) on: a disagreement with the ledger, a fill that took
        liquidity, money moved from outside the bot, the daily loss limit
    while not running, or with stale data, a dropped connection or the time budget not
        holding: cancel everything and quote nothing
    on a new ledger, with nothing unresolved: quote what `quotes` asks for

An offer is replaced when its price changes, or when little of its life is left, so it never
lapses while quoting. A new offer removes the one before it in the same transaction, and any
other expired offer is cancelled, because an expired offer stays on the ledger holding reserve
until something removes it. It always starts paused,
and a restart pauses it and cancels what was left, so only Ethan's `resume` starts quoting.
A server reporting another network turns on the kill switch and stops: nothing more is sent.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from debtlab.money import D, plain
from debtlab.xrpl_market import closetimes, store
from debtlab.xrpl_market.live import LABEL, book, controls, guards
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live.guards import MainnetBlocked
from debtlab.xrpl_market.live.ledger import TestnetLedger
from debtlab.xrpl_market.live.risk import AmmQuotes, LiveMark, LiveRiskCheck, affordable, amm_prices, others_for, own_offers_problem
from debtlab.xrpl_market.live.signer import Submitter
from debtlab.xrpl_market.live.venue import LiveOrder, LiveVenue, unresolved
from debtlab.xrpl_market.paper import PAIR
from debtlab.xrpl_market.paper import market as mk
from debtlab.xrpl_market.readonly import XrplReadError
from debtlab.xrpl_market.view import open_readonly

DROPS = Decimal(1_000_000)


@dataclass(frozen=True)
class Want:
    """One side's quote."""
    price: Decimal
    size: Decimal
    paper_order_id: str | None = None
    decision_id: int | None = None


Quotes = Callable[[mk.Market, datetime], dict[str, Want | None]]


class LiveBot:
    def __init__(self, conn: sqlite3.Connection, monitor_path: Path | str, cfg: dict, ledger: TestnetLedger, venue: LiveVenue,
                 submitter: Submitter, quotes: Quotes, out: Callable[[str], None] = print, alongside=None):
        guards.check_config(cfg)
        if not Path(monitor_path).exists():
            raise FileNotFoundError(f"no monitor database at {monitor_path}: start the testnet monitor first")
        self.mon = open_readonly(Path(monitor_path))
        if store.network(self.mon) != "testnet":
            self.mon.close()
            raise ldb.WrongDatabase(f"{monitor_path} doesn't record testnet; use the testnet monitor's database")
        self.conn, self.cfg, self.ledger, self.venue, self.submitter = conn, cfg, ledger, venue, submitter
        self.quotes, self.out, self.alongside = quotes, out, alongside
        self.risk = LiveRiskCheck(cfg)
        self.max_age = D(cfg["paper"]["limits"]["max_data_age_seconds"])
        self.last_read: controls.Read | None = None
        self.synced: int | None = None
        self.refusals: dict = {}
        self.run_id: int | None = None
        self.down_since_cancel = False

    # --- reading -------------------------------------------------------------------------

    def _market(self, now: datetime) -> tuple[mk.Market, AmmQuotes | None]:
        self.mon.execute("BEGIN")
        try:
            return mk.read_market(self.mon, now, self.cfg), amm_prices(self.mon, now)
        finally:
            self.mon.execute("ROLLBACK")

    def _fee_drops(self, m: mk.Market | None) -> int:
        if m is None:
            return int(self.cfg["paper"]["network_fallback"]["fee_drops"])
        return int(m.fee_xrp * DROPS)

    # --- start and stop ------------------------------------------------------------------

    def start(self, now: datetime) -> int:
        self.ledger.check_network()
        ldb.seed_limits(self.conn, self.cfg, now)
        a = book.account(self.conn)
        if a and a["address"] != self.venue.signer.address:
            raise ldb.ControlError(f"this database is for account {a['address']}, not {self.venue.signer.address}")
        self.run_id, gap = ldb.start_run(self.conn, now, float(self.max_age))
        with ldb.tx(self.conn):
            if ldb.state(self.conn)["state"] == "running":
                ldb.set_state(self.conn, "paused", "bot", "restarted: quoting needs a resume", now)
            ldb.event(self.conn, now, "run_started", {"run": self.run_id, "seconds_since_last_alive": gap, "label": LABEL})
        m, _ = self._market(now)
        read = controls.read_ledger(self.conn, self.ledger, self.venue, self.cfg, m.mid, self._fee_drops(m), now)
        self._stops(read, m, now)
        self.last_read, self.synced = read, read.header.index
        if self.alongside:
            self.alongside.start(now)
        n = controls.cancel_offers(self.conn, self.venue, read, "restart: left by the previous run", now, include_expired=True)
        if n:
            self.out(f"{LABEL}: cancelling {n} offer(s) left by the previous run")
        return self.run_id

    def stop(self, clock: Callable[[], datetime], reason: str, sleep: Callable[[float], None] = time.sleep,
             timeout: float = 30.0) -> controls.Cleared:
        m = None
        try:
            m, _ = self._market(clock())
        except sqlite3.Error:
            pass
        cleared = controls.cancel_until_zero(self.conn, self.ledger, self.venue, self.cfg, f"bot stopped: {reason}",
                                             self._fee_drops(m), timeout=timeout, clock=clock, sleep=sleep)
        with ldb.tx(self.conn):
            ldb.event(self.conn, clock(), "run_stopped", {"run": self.run_id, "reason": reason, "zero_offers": cleared.zero,
                                                          "remaining": cleared.remaining})
        if self.run_id is not None:
            ldb.stop_run(self.conn, self.run_id, clock(), reason)
            if self.alongside:
                self.alongside.stop(clock(), reason)
        return cleared

    # --- the stops -----------------------------------------------------------------------

    def _pause(self, why: str, now: datetime) -> None:
        with ldb.tx(self.conn):
            if ldb.state(self.conn)["state"] == "running":
                ldb.set_state(self.conn, "paused", "bot", why, now)
        self.out(f"{LABEL}: {why}")

    def _stops(self, read: controls.Read, m: mk.Market, now: datetime) -> None:
        if read.problems:
            self._pause("the books disagree with the ledger: " + "; ".join(read.problems), now)
        for e in read.effects:
            if e.took:
                self._pause(f"a fill took liquidity (transaction {e.hash}): the rule is never to intend to take, and to stop if it does", now)
            if e.external:
                self._pause(f"funds moved from outside the bot (transaction {e.hash}: {plain(e.external_xrp)} XRP,"
                            f" {plain(e.external_rlusd)} RLUSD): look, then resume", now)

    def _daily_loss(self, m: mk.Market, now: datetime) -> None:
        if m.mid is None or m.fx is None or not book.account(self.conn):
            return
        cap = ldb.limits(self.conn).get("max_daily_loss_eur")
        day = book.daily_pnl_rlusd(self.conn, m.mid, now)
        if cap is not None and day is not None and m.to_eur(day) <= -cap:
            self._pause(f"daily loss EUR {-m.to_eur(day):.4f} has reached the maximum {cap}", now)

    # --- one tick ------------------------------------------------------------------------

    def tick(self, now: datetime) -> dict:
        ldb.heartbeat(self.conn, self.run_id, now)
        m, amm = self._market(now)
        out = {"ledger": m.ledger_index, "new_ledger": False, "placed": 0, "cancels": 0, "problems": []}
        if self.alongside and (why := self.alongside.tick(now, m)):
            self._pause(why, now)
        try:
            read, new = self._read(m, now)
        except XrplReadError as e:
            out["problems"] = [f"ledger read failed: {e}"]
            out["cancels"] = self._cancel_once_when_down(out["problems"][0], now)
            return out
        out["new_ledger"], out["ledger"] = new, read.header.index
        self.down_since_cancel = False
        if new:
            self._stops(read, m, now)
        if book.account(self.conn) and m.mid is not None and m.valid:
            with ldb.tx(self.conn):
                book.record_day_open(self.conn, m.mid, now)
        self._daily_loss(m, now)
        problems = m.problems(self.max_age)
        if (why := own_offers_problem(m, self.venue.signer.address)):
            problems.append(why)
        if self.venue.down and not self.submitter.alive():
            problems.append(f"connection: {self.venue.down}")
        elif self.venue.down:
            self.venue.down = None
        h = read.header
        if not closetimes.budget_holds(h.resolution, h.close_flags, self.cfg["offer_expiry"]):
            problems.append(f"close-time budget doesn't hold (resolution {h.resolution} s, flags {h.close_flags})")
        out["problems"] = problems
        st = ldb.state(self.conn)
        if st["state"] != "running":
            out["cancels"] = controls.cancel_offers(self.conn, self.venue, read, f"{st['state']}: {st['reason']}", now, include_expired=True)
            return out
        if not book.account(self.conn) and not problems and not self._open(read, m, now):
            return out
        if problems:
            out["cancels"] = controls.cancel_offers(self.conn, self.venue, read, "safety: " + "; ".join(problems), now, include_expired=True)
            return out
        if new and unresolved(self.conn) == 0:
            placed, cancels = self._quote(read, m, amm, now)
            out["placed"], out["cancels"] = placed, cancels
        return out

    def _read(self, m: mk.Market, now: datetime) -> tuple[controls.Read, bool]:
        index = self.ledger.client.validated_index()
        if self.last_read is not None and index <= self.synced:   # the same ledger, or an endpoint a little behind
            return self.last_read, False
        read = controls.read_ledger(self.conn, self.ledger, self.venue, self.cfg, m.mid, self._fee_drops(m), now)
        self.last_read, self.synced = read, read.header.index
        return read, True

    def _cancel_once_when_down(self, why: str, now: datetime) -> int:
        """No read means no fresh Sequence, so try once per outage from the last read. Offers expire by themselves anyway."""
        if self.down_since_cancel or self.last_read is None or self.venue.context is None:
            return 0
        self.down_since_cancel = True
        with ldb.tx(self.conn):
            ldb.event(self.conn, now, "connection_lost", {"why": why})
        self.out(f"{LABEL}: {why}")
        return controls.cancel_offers(self.conn, self.venue, self.last_read, "safety: " + why, now)

    def _open(self, read: controls.Read, m: mk.Market, now: datetime) -> bool:
        try:
            with ldb.tx(self.conn):
                book.open_account(self.conn, read.account, m.mid, m.fx.usd_per_eur, now)
                book.record_day_open(self.conn, m.mid, now)
            x, r = book.set_aside(self.conn)
            if x or r:
                self.out(f"{LABEL}: the account holds more than the budget; {x} XRP and {r} RLUSD set aside, not used")
            return True
        except ldb.ControlError as e:
            self._pause(f"account not opened: {e}", now)
            return False

    # --- quoting -------------------------------------------------------------------------

    def _quote(self, read: controls.Read, m: mk.Market, amm: AmmQuotes | None, now: datetime) -> tuple[int, int]:
        wants = self.quotes(m, now)
        mark = LiveMark(m, read.header, read.account, amm)
        placed = cancels = 0
        minimum = D(self.cfg["paper"]["strategy"]["min_order_xrp"])
        for side in ("buy", "sell"):
            want = wants.get(side)
            cur = book.open_orders(self.conn, side)
            current = cur[0] if cur else None
            if want is None:
                cancels += self._cancel_all(cur, "nothing to quote on this side", now)
                continue
            if current and D(current["price"]) == want.price and current["paper_order_id"] == want.paper_order_id \
                    and not closetimes.refresh_due(read.header.close_time, current["expiration"], self.cfg["offer_expiry"]):
                continue
            replacing = frozenset(o["id"] for o in cur)
            room = affordable(side, want.price, book.within_budget(self.conn, read.account), m, self.cfg,
                              others_for(self.conn, "", replacing))
            size = min(want.size, room)
            if size < minimum:
                size = want.size      # let the risk check say why
            order = LiveOrder(f"L{self.venue.context.next_sequence}-{side}-{read.header.index}", PAIR, side, want.price, size,
                              issuer=self.cfg["rlusd"]["issuer"], decision_id=want.decision_id,
                              paper_order_id=want.paper_order_id,
                              replaces=current["sequence"] if current else self._expired_on_ledger(read, side))
            reasons = self.risk.check(self.conn, order, {PAIR: mark}, now, replacing=replacing)
            if reasons:
                self._refused(order, reasons, now)
                cancels += self._cancel_all(cur, "refused: " + "; ".join(reasons), now)
                continue
            self.refusals.pop(side, None)
            self.venue.submit(self.conn, order, now)
            placed += 1
            cancels += self._cancel_all(cur[1:], "more than one order on a side", now)
        cancels += controls.cancel_expired(self.conn, self.venue, read, "expired, and nothing is replacing it", now)
        return placed, cancels

    @staticmethod
    def _expired_on_ledger(read: controls.Read, side: str) -> int | None:
        """An expired offer stays on the ledger, holding reserve, until something removes it. The next offer does."""
        gone = [o.sequence for o in read.account.offers if o.side == side and o.expiration is not None
                and o.expiration <= read.header.close_time]
        return gone[0] if gone else None

    def _cancel_all(self, orders: list[dict], reason: str, now: datetime) -> int:
        for o in orders:
            self.venue.cancel_offer(self.conn, o["sequence"], reason, now, o["id"])
        return len(orders)

    def _refused(self, order: LiveOrder, reasons: list[str], now: datetime) -> None:
        prev = self.refusals.get(order.side)
        if prev and prev[0] == tuple(reasons):
            prev[1] += 1      # the same refusal again: counted, not re-logged
            return
        with ldb.tx(self.conn):
            ldb.event(self.conn, now, "order_refused", {"side": order.side, "price": plain(order.price), "size": plain(order.size),
                                                        "reasons": reasons, "previous_refusal_repeated": prev[1] if prev else 0,
                                                        "label": LABEL})
        self.refusals[order.side] = [tuple(reasons), 0]

    # --- running -------------------------------------------------------------------------

    def _refuse(self, e: MainnetBlocked, clock: Callable[[], datetime]) -> None:
        controls.set_killed(self.conn, "bot", f"refused: {e}", clock())
        if self.run_id is not None:
            ldb.stop_run(self.conn, self.run_id, clock(), f"refused: {e}")
        self.out(f"{LABEL}: REFUSED: {e}. Kill switch on; nothing more is sent.")

    def _crashed(self, e: Exception, clock: Callable[[], datetime], sleep: Callable[[float], None]) -> None:
        """Anything unexpected: pause, say so, and cancel as far as possible, then let the error show."""
        why = f"crashed: {type(e).__name__}: {e}"
        self.out(f"{LABEL}: {why}. Cancelling everything.")
        try:
            controls.pause(self.conn, "bot", why, clock())
            with ldb.tx(self.conn):
                ldb.event(self.conn, clock(), "run_crashed", {"run": self.run_id, "why": why})
            cleared = self.stop(clock, why, sleep)
            self.out(f"{LABEL}: " + ("no offers on the ledger." if cleared.zero else
                     f"{cleared.remaining} offer(s) still listed; each expires by itself."))
        except Exception as e2:
            self.out(f"{LABEL}: couldn't finish cancelling ({type(e2).__name__}); each offer expires by itself within "
                     f"{self.cfg['offer_expiry']['max_live_seconds']} s.")

    def run(self, until: datetime | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            sleep: Callable[[float], None] = time.sleep) -> str:
        reason = "finished (--for)"
        tick_s = float(self.cfg["paper"]["tick_seconds"])
        try:
            self.start(clock())
            self.out(f"{LABEL}: started (run {self.run_id}), account {self.venue.signer.address}, state "
                     f"{ldb.state(self.conn)['state']}. Quoting only starts after `resume`.")
            while until is None or clock() < until:
                self.tick(clock())
                sleep(tick_s)
        except KeyboardInterrupt:
            reason = "stopped by user"
        except MainnetBlocked as e:
            self._refuse(e, clock)
            raise
        except Exception as e:
            self._crashed(e, clock, sleep)
            raise
        try:
            cleared = self.stop(clock, reason, sleep)
        except MainnetBlocked as e:
            self._refuse(e, clock)
            raise
        self.out(f"{LABEL}: stopped ({reason}); " + ("no offers on the ledger." if cleared.zero else
                 f"{cleared.remaining} offer(s) still listed ({cleared.last_error}); each expires by itself."))
        return reason
