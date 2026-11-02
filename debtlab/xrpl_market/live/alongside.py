"""Paper alongside the testnet bot (M8 step 7): both fill rules on exactly the same quotes.

The paper bot runs unchanged on the testnet monitor's data, on its own database. It decides
the quotes, and the live offers follow the prices of its driver account's open orders, so
live and paper are comparable fill by fill. State is kept the same both ways: a live stop
pauses paper, and a breach on a paper account pauses live.

A paper database used here is marked as testnet, and the M7 paper command refuses one with
that mark, so testnet fills can't land in the M7 paper evidence, or the other way round.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from debtlab.money import D
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live.bot import Want
from debtlab.xrpl_market.paper import account, controls as pcontrols
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.paper import market as mk
from debtlab.xrpl_market.paper.bot import PaperBot


def open_testnet_paper(path: Path | str) -> sqlite3.Connection:
    """A paper database for testnet: new, or marked as testnet already. Refuses one with M7 evidence in it."""
    conn = pdb.open_db(path)
    mark = conn.execute("SELECT value FROM paper_meta WHERE key = 'network'").fetchone()
    if mark is None:
        used = conn.execute("SELECT 1 FROM paper_state_changes LIMIT 1").fetchone() or conn.execute(
            "SELECT 1 FROM paper_runs LIMIT 1").fetchone()
        if used:
            conn.close()
            raise pdb.WrongDatabase(f"{path} has been used for M7 paper trading; testnet uses its own paper database")
        conn.execute("INSERT INTO paper_meta(key, value) VALUES ('network', 'testnet')")
        conn.commit()
    elif mark["value"] != "testnet":
        conn.close()
        raise pdb.WrongDatabase(f"{path} records {mark['value']}, not testnet")
    return conn


class Alongside:
    def __init__(self, live: sqlite3.Connection, paper: sqlite3.Connection, monitor_path: Path | str, cfg: dict,
                 out=print):
        self.live, self.paper, self.cfg = live, paper, cfg
        self.bot = PaperBot(paper, monitor_path, cfg, out=out)
        self.driver = cfg["paper"]["driver_account"]

    def start(self, now: datetime) -> None:
        self.bot.start(now)

    def stop(self, now: datetime, reason: str) -> None:
        self.bot.stop(now, reason)

    def tick(self, now: datetime, m: mk.Market) -> str | None:
        """Keep the states together, then run paper. Returns why live must pause, if paper has a breach."""
        why = self._mirror(now, m)
        self.bot.tick(now)
        return why or self._paper_breach()

    def _paper_breach(self) -> str | None:
        p = pdb.state(self.paper)
        if ldb.state(self.live)["state"] == "running" and p["state"] == "paused" and p["changed_by"] == "risk check":
            return f"paper: {p['reason']}"
        return None

    def _mirror(self, now: datetime, m: mk.Market) -> str | None:
        live, paper = ldb.state(self.live)["state"], pdb.state(self.paper)["state"]
        if live == "killed" and paper != "killed":
            pcontrols.kill(self.paper, "live bot", "live kill switch", now, m)
        elif live == "paused" and paper == "running":
            pcontrols.pause(self.paper, "live bot", "live is paused", now, m)
        elif live == "paused" and paper == "killed":
            pcontrols.reset(self.paper, "live bot", "live kill switch reset", now)
        elif live == "running" and paper == "paused" and pdb.state(self.paper)["changed_by"] != "risk check":
            pcontrols.resume(self.paper, "live bot", "live is running", now)
        elif live == "running" and paper == "killed":
            return "paper's kill switch is on"
        return None

    def wants(self, m: mk.Market, now: datetime) -> dict[str, Want | None]:
        """Each side's quote: the driver paper account's open order, if there is one."""
        out: dict[str, Want | None] = {"buy": None, "sell": None}
        for o in account.open_orders(self.paper, self.driver):
            out[o["side"]] = Want(D(o["price"]), D(o["remaining"]), o["id"], o["decision_id"])
        return out
