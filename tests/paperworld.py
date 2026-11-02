"""A small fake world for the paper bot's tests: a real monitor database written through
`store` (snapshots at controlled times, trades, the ECB rate, reserves, heartbeat) and a
paper database. Nothing touches a network."""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from debtlab.xrpl_market import book, load_config, store
from debtlab.xrpl_market.fx import FxRate
from debtlab.xrpl_market.paper import controls
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.paper.bot import PaperBot
from debtlab.xrpl_market.trades import Trade
from debtlab.xrpl_market.verify import NetworkCosts

D = Decimal
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
CFG = load_config()
RLUSD, ISSUER = CFG["rlusd"]["currency_hex"], CFG["rlusd"]["issuer"]


def rl(v):
    return {"currency": RLUSD, "issuer": ISSUER, "value": str(v)}


def mkbook(ledger, bid, ask, size=100):
    bid, ask = D(bid), D(ask)
    bids = {"ledger_index": ledger, "validated": True,
            "offers": [{"TakerGets": rl(bid * size), "TakerPays": str(size * 1_000_000)}]}
    asks = {"ledger_index": ledger, "validated": True,
            "offers": [{"TakerGets": str(size * 1_000_000), "TakerPays": rl(ask * size)}]}
    return book.parse_book([bids], [asks], RLUSD)


class World:
    def __init__(self, tmp_path, vol_min_samples=3, fx_date=date(2026, 10, 30)):
        self.cfg = copy.deepcopy(CFG)
        self.cfg["paper"]["strategy"]["vol_min_samples"] = vol_min_samples
        self.mon_path = tmp_path / "xrpl.sqlite3"
        self.mon = store.open_db(self.mon_path)
        self.t, self.ledger = T0, 1000
        store.record_fx(self.mon, FxRate(fx_date, D("1.25"), "ECB"), T0)
        self.run = store.start_run(self.mon, T0)
        store.record_network(self.mon, NetworkCosts(1000, D(1), D("0.2"), D(10), D(10)), T0)
        self.paper_path = tmp_path / "paper.sqlite3"
        self.paper = pdb.open_db(self.paper_path)
        self.lines: list[str] = []
        self.ntx = 0

    def close(self, bid="1.249", ask="1.251", trades=(), dt=4.0):
        """The next validated ledger: its trades (venue, taker_side, price, xrp), then its snapshot."""
        self.ledger += 1
        self.t += timedelta(seconds=dt)
        ts = []
        for venue, side, price, xrp in trades:
            self.ntx += 1
            p, x = D(price), D(xrp)
            ts.append(Trade(f"{self.ntx:064X}", "amm" if venue == "amm" else f"OFF{self.ntx}", self.ledger, venue,
                            side, p, x, p * x, "rMaker", "OfferCreate"))
        if ts:
            store.record_trades(self.mon, ts, self.t - timedelta(seconds=1))
        store.record_snapshot(self.mon, self.run, "wss://test", self.ledger, self.t - timedelta(seconds=1), self.t,
                              "top", mkbook(self.ledger, bid, ask), None)
        return self.ledger

    def warm(self, n=5, **kw):
        for _ in range(n):
            self.close(**kw)

    def at(self, s=0.5):
        return self.t + timedelta(seconds=s)

    def bot(self) -> PaperBot:
        return PaperBot(self.paper, self.mon_path, self.cfg, out=self.lines.append)

    def running_bot(self) -> PaperBot:
        """Warm history, a started bot, resumed, and a first tick: accounts open and quoted."""
        self.warm()
        b = self.bot()
        b.start(self.at(0.2))
        controls.resume(self.paper, "Ethan", "test", self.at(0.3))
        b.tick(self.at())
        return b

    def orders(self, status="open"):
        return [dict(r) for r in self.paper.execute("SELECT * FROM paper_orders WHERE status = ? ORDER BY id", (status,))]

    def events(self, kind):
        return [dict(r) for r in self.paper.execute("SELECT * FROM paper_events WHERE kind = ? ORDER BY id", (kind,))]
