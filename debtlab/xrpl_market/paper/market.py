"""What the paper bot knows about RLUSD/XRP at a moment, read from the monitor's database.

The monitor's database is opened read-only (`view.open_readonly`). Everything is read *as
of* `now`: only rows received by then count, so the same code runs live and replays
recorded data in tests without seeing the future.

"Connected" means the monitor is alive (heartbeat within the data-age limit) and no
monitor outage is open at `now`. A monitor that stops, loses its connection or gets no
validated ledger therefore makes the paper bot cancel everything, as the live rule says.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from debtlab.money import D
from debtlab.xrpl_market.fx import FxRate
from debtlab.xrpl_market.store import iso, parse

DROPS = Decimal(1_000_000)


@dataclass(frozen=True)
class Market:
    now: datetime
    ledger_index: int | None
    received_at: datetime | None
    valid: bool
    reasons: tuple[str, ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    connected: bool
    connection: str                       # why not connected, or "monitor alive, no outage open"
    fx: FxRate | None
    mids: tuple[Decimal, ...]             # valid mids in the volatility window, oldest first
    reserve_base_xrp: Decimal
    reserve_inc_xrp: Decimal
    fee_xrp: Decimal                      # network fee for one transaction (place or cancel)
    network_source: str
    currency: str = field(default="RLUSD")  # an `interfaces.Mark`: prices and values are in RLUSD

    @property
    def age_s(self) -> float | None:
        return None if self.received_at is None else (self.now - self.received_at).total_seconds()

    @property
    def fx_stale(self) -> bool:
        return self.fx is None or self.fx.stale(self.now)

    def problems(self, max_age_s: Decimal) -> list[str]:
        """Every reason not to have orders resting now (the live rule plus a usable book and rate)."""
        out = []
        if self.ledger_index is None:
            out.append("no snapshot from the monitor yet")
        else:
            if self.age_s is not None and self.age_s > float(max_age_s):
                out.append(f"data {self.age_s:.0f}s old; the maximum is {max_age_s}s")
            if not self.valid:
                out.append("book invalid: " + "; ".join(self.reasons))
        if not self.connected:
            out.append("connection: " + self.connection)
        if self.fx is None:
            out.append("no EUR/USD rate")
        elif self.fx_stale:
            out.append(f"EUR/USD rate of {self.fx.obs_date} is stale")
        return out

    def to_eur(self, rlusd: Decimal) -> Decimal:
        return rlusd / self.fx.usd_per_eur        # 1 RLUSD = 1 USD (decision D, risk R19)

    def from_eur(self, eur: Decimal) -> Decimal:
        return eur * self.fx.usd_per_eur


def read_market(mon: sqlite3.Connection, now: datetime, cfg: dict) -> Market:
    p = cfg["paper"]
    t = iso(now)
    snap = mon.execute("SELECT * FROM xrpl_snapshots WHERE received_at <= ? ORDER BY ledger_index DESC LIMIT 1",
                       (t,)).fetchone()
    window = iso(now - timedelta(minutes=p["strategy"]["vol_window_minutes"]))
    mids = tuple(D(r["mid"]) for r in mon.execute(
        "SELECT mid FROM xrpl_snapshots WHERE valid = 1 AND received_at > ? AND received_at <= ? ORDER BY ledger_index",
        (window, t)))
    fxr = mon.execute("SELECT * FROM fx_rates WHERE fetched_at <= ? ORDER BY obs_date DESC LIMIT 1", (t,)).fetchone()
    fx = None if fxr is None else FxRate(datetime.fromisoformat(fxr["obs_date"]).date(), D(fxr["rate"]), fxr["source"])
    net = mon.execute("SELECT * FROM xrpl_network WHERE checked_at <= ? ORDER BY ledger_index DESC LIMIT 1",
                      (t,)).fetchone()
    if net:
        base, inc, fee = D(net["reserve_base_xrp"]), D(net["reserve_inc_xrp"]), D(net["current_fee_drops"]) / DROPS
        source = f"ledger {net['ledger_index']}"
    else:
        fb = p["network_fallback"]
        base, inc, fee = D(fb["reserve_base_xrp"]), D(fb["reserve_inc_xrp"]), D(fb["fee_drops"]) / DROPS
        source = "config fallback (no network read yet)"
    connected, why = _connection(mon, now, D(p["limits"]["max_data_age_seconds"]))
    if snap is None:
        return Market(now, None, None, False, ("no snapshot",), None, None, None, connected, why, fx, mids,
                      base, inc, fee, source)
    dec = (lambda x: None if x is None else D(x))
    return Market(now, snap["ledger_index"], parse(snap["received_at"]), bool(snap["valid"]),
                  tuple(json.loads(snap["reasons"])), dec(snap["best_bid"]), dec(snap["best_ask"]), dec(snap["mid"]),
                  connected, why, fx, mids, base, inc, fee, source)


def _connection(mon: sqlite3.Connection, now: datetime, max_age: Decimal) -> tuple[bool, str]:
    hb = mon.execute("SELECT at FROM xrpl_heartbeat WHERE id = 1").fetchone()
    if hb is None:
        return False, "the monitor has never run"
    age = (now - parse(hb["at"])).total_seconds()
    if age > float(max_age):
        return False, f"no monitor heartbeat for {age:.0f}s"
    t = iso(now)
    o = mon.execute("SELECT kind, detail FROM xrpl_outages WHERE started_at <= ? AND (ended_at IS NULL OR ended_at > ?)"
                    " ORDER BY id DESC LIMIT 1", (t, t)).fetchone()
    if o:
        return False, f"monitor outage open ({o['kind']}: {o['detail']})"
    return True, "monitor alive, no outage open"


def realised_vol_bp(mids: tuple[Decimal, ...]) -> tuple[Decimal | None, int]:
    """Standard deviation of the log change in mid from one ledger to the next, in bp, and
    how many changes it is based on."""
    rets = [math.log(float(b / a)) for a, b in zip(mids, mids[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None, len(rets)
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return D(f"{math.sqrt(var) * 10_000:.6f}"), len(rets)


def trades(mon: sqlite3.Connection, after_ledger: int, upto_ledger: int, now: datetime) -> list[sqlite3.Row]:
    return mon.execute("SELECT * FROM xrpl_trades WHERE ledger_index > ? AND ledger_index <= ? AND received_at <= ?"
                       " ORDER BY ledger_index, rowid", (after_ledger, upto_ledger, iso(now))).fetchall()


def mid_at_ledger(mon: sqlite3.Connection, ledger: int) -> Decimal | None:
    r = mon.execute("SELECT mid FROM xrpl_snapshots WHERE valid = 1 AND ledger_index <= ? ORDER BY ledger_index DESC"
                    " LIMIT 1", (ledger,)).fetchone()
    return None if r is None else D(r["mid"])


def mid_after(mon: sqlite3.Connection, t: datetime, now: datetime) -> tuple[Decimal, datetime] | None:
    """The first valid mid received at or after `t` (and by `now`): for markouts."""
    r = mon.execute("SELECT mid, received_at FROM xrpl_snapshots WHERE valid = 1 AND received_at >= ? AND received_at <= ?"
                    " ORDER BY received_at LIMIT 1", (iso(t), iso(now))).fetchone()
    return None if r is None else (D(r["mid"]), parse(r["received_at"]))
