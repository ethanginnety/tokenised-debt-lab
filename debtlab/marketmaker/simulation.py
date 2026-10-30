"""The simulated market (M4 step 5). SIMULATED: its P&L is not evidence of real profitability.

One run is a trading day of one-minute steps on a simulated clock. The government
curve moves as a seeded random walk with occasional jumps (news), in bp, on top of
the real curve. The bot sees each move `lag_steps` late and requotes every
`requote_every` steps, through the same quote decisions and execution check as
always. Clients arrive at random:

- **Uninformed** clients buy or sell at random and are price-sensitive: the chance
  they deal is exp(-k x distance of the quote from fair value in bp), so a wider
  quote earns more per fill but gets fewer fills (Ethan's change 1).
- **Informed** clients know the true value now. They deal only when the bot's
  quote is stale against it, and take everything offered: adverse selection. They
  arrive on top of the uninformed ones, with their own chance each step, so adding
  them takes no volume away from the uninformed flow: the difference between runs
  with and without them is adverse selection, not lost volume (Ethan's change).

Each fill is a ledger trade (clean + accrued), plus a fee. At the end, the bot can
unwind inventory above its target against an outside market, paying its spread and
a size-dependent impact: slippage. The unwind runs at the end of a run if the config
asks and the bot is running or paused; after a kill it runs only on an explicit
command (`unwind`), never by itself (Ethan's change 3).

Calibration comes in named profiles (Ethan's change 1): *normal*, about 8 bp a day of
yield standard deviation, close to real 30-year gilts, and *stress*, with five times
as many jumps, for showing adverse selection. `daily_sd_bp` gives what a profile implies.

Every random draw comes from the seed, the step
and the instrument, so the same seed gives the same run, and a run that crashes is
finished by running it again: each step's quotes and fills commit together, and the
run continues from the step after the last one committed.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from functools import lru_cache
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from debtlab import ledger, lifecycle
from debtlab.audit.db import atomic, record_event
from debtlab.instruments import Instrument
from debtlab.ledger import Line
from debtlab.marketdata.models import CurveSnapshot
from debtlab.marketmaker import controls, execution, pnl
from debtlab.marketmaker.execution import Mark, Order
from debtlab.marketmaker.quotes import load_quote_params
from debtlab.marketmaker.quoting import decide_quotes
from debtlab.money import D, HUNDRED, ONE, ZERO, money, plain
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import value_bond

BP_PER_UNIT = Decimal(10_000)
LABEL = "SIMULATED: not evidence of real profitability"
MARKOUT_STEPS = (5, 30)  # horizons, in steps after the fill, capped at the run's last step
CLIENTS = ("uninformed", "informed", "unwind")


class SimulationError(RuntimeError):
    pass


class SimulatedCrash(RuntimeError):
    """Raised by tests part-way through a step, to prove a rerun finishes the run."""


@dataclass(frozen=True)
class SimParams:
    steps: int
    step_seconds: int
    requote_every: int
    lag_steps: int
    vol_bp_per_step: Decimal  # yield, normal increments
    jump_prob: Decimal  # chance of a jump each step
    jump_bp: Decimal  # yield, up or down
    arrival_prob: Decimal  # chance a client arrives for each bond each step
    informed_arrival_prob: Decimal  # chance an informed client arrives for each bond each step, on top
    k: dict[str, Decimal]  # fill-rate decay per bp, per instrument
    unwind_at_end: bool
    outside_half_spread_bp: dict[str, Decimal]  # of price, per instrument
    impact_bp_per_million: Decimal  # of price, per million face unwound
    profile: str = "normal"

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "SimParams":
        s = json.loads(text)
        return _params(s, s.pop("profile"))


def load_sim_params(path: Path, profile: str = "normal", **overrides) -> SimParams:
    """The config's simulation settings, then the named profile's, then `overrides`."""
    s = {k: v for k, v in json.loads(Path(path).read_text(encoding="utf-8"))["simulation"].items()
         if not k.startswith("_")}
    profiles = s.pop("profiles")
    if profile not in profiles:
        raise SimulationError(f"no simulation profile {profile!r} (have {', '.join(sorted(profiles))})")
    s.update({k: v for k, v in profiles[profile].items() if not k.startswith("_")})
    s.update(overrides)
    return _params(s, profile)


def _params(s: dict, profile: str) -> SimParams:
    return SimParams(
        steps=int(s["steps"]), step_seconds=int(s["step_seconds"]), requote_every=int(s["requote_every"]),
        lag_steps=int(s["lag_steps"]), vol_bp_per_step=D(s["vol_bp_per_step"]), jump_prob=D(s["jump_prob"]),
        jump_bp=D(s["jump_bp"]), arrival_prob=D(s["arrival_prob"]), informed_arrival_prob=D(s["informed_arrival_prob"]),
        k={i: D(v) for i, v in s["k"].items()}, unwind_at_end=bool(s["unwind_at_end"]),
        outside_half_spread_bp={i: D(v) for i, v in s["outside_half_spread_bp"].items()},
        impact_bp_per_million=D(s["impact_bp_per_million"]), profile=profile,
    )


def daily_sd_bp(params: SimParams) -> Decimal:
    """Standard deviation of the day's yield move, in bp, that the parameters imply:
    each step adds a normal move (variance vol^2) and, with probability p, a jump of
    +-J either way (variance p x J^2), independently, over `steps` steps."""
    per_step = params.vol_bp_per_step ** 2 + params.jump_prob * params.jump_bp ** 2
    return (params.steps * per_step).sqrt()


@lru_cache(maxsize=None)
def _sizes_and_fees(config_path: Path, instrument_id: str):
    """Quote parameters for sizes, fees and target (maximum inventory isn't used here)."""
    return load_quote_params(config_path, instrument_id, ZERO)


def fill_probability(distance_bp: Decimal, k: Decimal) -> Decimal:
    """Chance an uninformed client deals at a quote `distance_bp` (of price) from fair value."""
    return (-k * abs(distance_bp)).exp()


def benchmark_paths(seed: str, params: SimParams, currencies: list[str]) -> dict[str, list[Decimal]]:
    """True cumulative move in bp at each step, per currency (0 at step 0)."""
    out = {}
    for ccy in currencies:
        rng = random.Random(f"{seed}:walk:{ccy}")
        level, path = ZERO, [ZERO]
        for _ in range(1, params.steps + 1):
            move = D(f"{rng.gauss(0, float(params.vol_bp_per_step)):.6f}")
            jump = rng.random() < float(params.jump_prob)
            direction = ONE if rng.random() < 0.5 else -ONE
            level += move + (direction * params.jump_bp if jump else ZERO)
            path.append(level)
        out[ccy] = path
    return out


class _Valuer:
    """Fair value per 100 on a shifted curve, by the bond's lifecycle state (cached)."""

    def __init__(self, conn: sqlite3.Connection, curves: dict[str, CurveSnapshot], on: date) -> None:
        self.conn, self.curves, self.on, self.cache = conn, curves, on, {}

    def __call__(self, bond: FixedRateBond, shift_bp: Decimal) -> tuple[Decimal, Decimal]:
        """(clean, accrued) per 100, accrued 0 when the bond trades flat."""
        key = (bond.id, shift_bp)
        if key not in self.cache:
            sp = lifecycle.scenario_price(self.conn, bond, self.curves[bond.benchmark].curve.shifted(shift_bp), self.on)
            if "clean" not in sp:
                raise SimulationError(f"{bond.id} can't be valued: {sp.get('message')}")
            self.cache[key] = (D(sp["clean"]), D(sp["accrued"]))
        return self.cache[key]


def run(conn: sqlite3.Connection, run_id: str, seed: str, instruments: list[Instrument],
        curves: dict[str, CurveSnapshot], on: date, start: datetime, max_curve_age_hours: int,
        config_path: Path, params: SimParams, crash_at_step: int | None = None) -> dict:
    """Run (or resume) a simulation and return its summary."""
    bonds = [i for i in instruments if isinstance(i, FixedRateBond) and i.benchmark in curves]
    mm = execution.market_maker_id(conn)
    market = conn.execute("SELECT id FROM parties WHERE role = 'market'").fetchone()["id"]
    value = _Valuer(conn, curves, on)
    currencies = sorted({b.currency for b in bonds})
    paths = benchmark_paths(seed, params, currencies)
    row = _start(conn, run_id, seed, params, bonds, mm, value, start)

    dv01 = {b.id: value_bond(b, curves[b.benchmark].curve, on).risk.dv01 for b in bonds}
    for step in range(row["last_step"] + 1, params.steps):
        now = start + timedelta(seconds=step * params.step_seconds)
        seen = {c: paths[c][max(step - params.lag_steps, 0)] for c in currencies}
        true = {c: paths[c][step] for c in currencies}
        with atomic(conn):
            if step % params.requote_every == 0:
                _requote(conn, run_id, step, instruments, bonds, curves, seen, on, now, max_curve_age_hours,
                         config_path, value, dv01, mm)
            for bond in bonds:
                _clients(conn, run_id, seed, step, bond, params, value, seen, true, on, now, mm, market, config_path)
                if crash_at_step == step:
                    raise SimulatedCrash(f"simulated crash at step {step}")
            conn.execute("UPDATE mm_runs SET last_step = ? WHERE id = ?", (step, run_id))

    with atomic(conn):
        if conn.execute("SELECT status FROM mm_runs WHERE id = ?", (run_id,)).fetchone()["status"] == "running":
            note = "not requested"
            if params.unwind_at_end:
                if controls.state(conn)["state"] == "killed":
                    note = "skipped: the bot is killed, so it unwinds only on an explicit command"
                else:
                    _unwind(conn, run_id, params, bonds, value, paths, on, start, mm, market, config_path)
                    note = "at the end of the run"
            controls.cancel_all(conn, f"end of simulation run {run_id}")
            summary = summarise(conn, run_id, bonds, value, paths, params, mm, note)
            conn.execute("UPDATE mm_runs SET status = 'finished', summary = ? WHERE id = ?",
                         (json.dumps(summary, default=str), run_id))
            record_event(conn, "simulation_finished", {"run": run_id, "label": LABEL, **summary},
                         idempotency_key=f"simulation_finished:{run_id}")
    return json.loads(conn.execute("SELECT summary FROM mm_runs WHERE id = ?", (run_id,)).fetchone()["summary"])


def unwind(conn: sqlite3.Connection, run_id: str, instruments: list[Instrument],
           curves: dict[str, CurveSnapshot], on: date, config_path: Path, by: str, reason: str) -> dict:
    """Unwind a finished run's inventory above target, on an explicit command: the only
    way to unwind after a kill. Only the latest run, once. Returns the new summary."""
    if not reason.strip():
        raise SimulationError("an unwind needs a reason")
    row = conn.execute("SELECT * FROM mm_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SimulationError(f"no simulation run {run_id}")
    if row["status"] != "finished":
        raise SimulationError(f"run {run_id} hasn't finished: run it again to finish it first")
    latest = conn.execute("SELECT id FROM mm_runs ORDER BY rowid DESC LIMIT 1").fetchone()["id"]
    if latest != run_id:
        raise SimulationError(f"run {run_id} isn't the latest run ({latest}); its inventory has moved on")
    if conn.execute("SELECT 1 FROM mm_fills WHERE run_id = ? AND client = 'unwind'", (run_id,)).fetchone():
        raise SimulationError(f"run {run_id} has already been unwound")
    params = SimParams.from_json(row["params"])
    bonds = [i for i in instruments if isinstance(i, FixedRateBond) and i.benchmark in curves]
    mm = execution.market_maker_id(conn)
    market = conn.execute("SELECT id FROM parties WHERE role = 'market'").fetchone()["id"]
    value = _Valuer(conn, curves, on)
    paths = benchmark_paths(row["seed"], params, sorted({b.currency for b in bonds}))
    with atomic(conn):
        _unwind(conn, run_id, params, bonds, value, paths, on, datetime.fromisoformat(row["started_at"]),
                mm, market, config_path)
        summary = summarise(conn, run_id, bonds, value, paths, params, mm, f"on command by {by}: {reason}")
        conn.execute("UPDATE mm_runs SET summary = ? WHERE id = ?", (json.dumps(summary, default=str), run_id))
        record_event(conn, "simulation_unwound", {"run": run_id, "by": by, "reason": reason, "label": LABEL,
                                                  "pnl": summary["pnl"]}, idempotency_key=f"simulation_unwound:{run_id}")
    return summary


def _start(conn, run_id, seed, params, bonds, mm, value, start) -> dict:
    row = conn.execute("SELECT * FROM mm_runs WHERE id = ?", (run_id,)).fetchone()
    if row:
        if (row["seed"], row["params"]) != (seed, params.to_json()):
            raise SimulationError(f"run {run_id} exists with a different seed or parameters")
        return dict(row)
    held = ledger.balances(conn).get(mm, {})
    state = {
        "units": {b.id: plain(held.get(b.id, ZERO)) for b in bonds},
        "cash": {c: plain(held.get(c, ZERO)) for c in {b.currency for b in bonds}},
        "marks": {b.id: plain(sum(value(b, ZERO))) for b in bonds},  # dirty, nothing seen yet
    }
    with atomic(conn):
        conn.execute("INSERT INTO mm_runs(id, seed, params, start, started_at, status) VALUES (?, ?, ?, ?, ?, 'running')",
                     (run_id, seed, params.to_json(), json.dumps(state), start.isoformat()))
        record_event(conn, "simulation_started", {"run": run_id, "seed": seed, "label": LABEL},
                     idempotency_key=f"simulation_started:{run_id}")
    return dict(conn.execute("SELECT * FROM mm_runs WHERE id = ?", (run_id,)).fetchone())


def _marks(conn, bonds, value, seen, dv01, now, curves) -> dict[str, Mark]:
    out = {}
    for b in bonds:
        clean, accrued = value(b, seen[b.currency])
        out[b.id] = Mark(b.currency, clean + accrued, accrued, dv01[b.id], D(str(curves[b.benchmark].age_hours(now))))
    return out


def running_pnl(conn, run_id: str, bonds, value, seen, mm) -> dict[str, Decimal]:
    """Total P&L so far per currency, at the bot's own marks: what the daily-loss limit sees."""
    start = json.loads(conn.execute("SELECT start FROM mm_runs WHERE id = ?", (run_id,)).fetchone()["start"])
    held = ledger.balances(conn).get(mm, {})
    currencies = {b.id: b.currency for b in bonds}
    end_marks = {b.id: sum(value(b, seen[b.currency])) for b in bonds}
    return pnl.total(currencies, {c: D(v) for c, v in start["cash"].items()}, held,
                     {i: D(v) for i, v in start["units"].items()}, held,
                     {i: D(v) for i, v in start["marks"].items()}, end_marks)


def _requote(conn, run_id, step, instruments, bonds, curves, seen, on, now, max_age, config_path, value, dv01, mm):
    shifted = {c: replace(s, source=f"{s.source} (simulated shift {seen[c]:+.4f} bp)", curve=s.curve.shifted(seen[c]))
               for c, s in curves.items() if c in seen}
    daily = running_pnl(conn, run_id, bonds, value, seen, mm)
    controls.cancel_all(conn, "requote", now)  # pull every old quote first, then quote afresh
    decisions = decide_quotes(conn, instruments, shifted, on, now, max_age, config_path, daily)
    marks = _marks(conn, bonds, value, seen, dv01, now, curves)
    for d in decisions:
        if d.quote is None:
            continue
        for side, price, size in (("buy", d.quote.bid, d.quote.bid_size), ("sell", d.quote.ask, d.quote.ask_size)):
            if price is not None:
                execution.place_order(conn, Order(f"{run_id}:{step}:{d.instrument_id}:{side}", d.instrument_id,
                                                  side, price, size), marks, daily, now)


def _clients(conn, run_id, seed, step, bond, params, value, seen, true, on, now, mm, market, config_path):
    """This step's clients for one bond: an informed one first (it is faster), then an
    uninformed one on what is left. Each has its own random stream, so turning informed
    clients on leaves every uninformed draw unchanged."""
    rng = random.Random(f"{seed}:{step}:{bond.id}")
    arrives = rng.random() < float(params.arrival_prob)
    client_buys = rng.random() < 0.5
    qp = _sizes_and_fees(config_path, bond.id)
    size = rng.randrange(int(qp.min_order / qp.size_step), int(qp.quote_size / qp.size_step) + 1) * qp.size_step
    deal_draw = D(f"{rng.random():.12f}")
    informed = random.Random(f"{seed}:{step}:{bond.id}:informed").random() < float(params.informed_arrival_prob)
    if not (arrives or informed):
        return
    seen_clean, accrued = value(bond, seen[bond.currency])
    true_clean, _ = value(bond, true[bond.currency])

    def orders():
        return {o["side"]: o for o in execution.open_orders(conn) if o["instrument_id"] == bond.id}

    def fill(order, amount, client):
        _fill(conn, run_id, step, bond, order["side"], D(order["price"]), amount, client,
              seen_clean + accrued, true_clean + accrued, on, now, mm, market, config_path, order["id"])

    if informed:  # deals only when the bot's quote is stale against the true value, and takes it all
        ask, bid = orders().get("sell"), orders().get("buy")
        if ask and true_clean > D(ask["price"]):
            fill(ask, ask["remaining"], "informed")
        elif bid and true_clean < D(bid["price"]):
            fill(bid, bid["remaining"], "informed")
    if arrives:
        order = orders().get("sell" if client_buys else "buy")
        if order is None:
            return
        distance = abs(D(order["price"]) - seen_clean) / seen_clean * BP_PER_UNIT
        if deal_draw < fill_probability(distance, params.k[bond.id]):
            fill(order, min(size, order["remaining"]), "uninformed")


def _fill(conn, run_id, step, bond, side, price, size, client, bot_fair, true_fair, on, now, mm, market,
          config_path, order_id=None, fill_id=None) -> None:
    fill_id = fill_id or f"{run_id}:{step}:{bond.id}:{side}:{client}"
    buyer, seller = (mm, market) if side == "buy" else (market, mm)
    s = ledger.trade(conn, f"fill:{fill_id}", bond, buyer, seller, size, price, on, memo=LABEL)
    fee_bp = _sizes_and_fees(config_path, bond.id).fee_bp
    fee = money(s.cash * fee_bp / BP_PER_UNIT)
    funding = conn.execute("SELECT id FROM parties WHERE role = 'funding'").fetchone()["id"]
    if fee:
        ledger.post(conn, f"fee:{fill_id}", "fee", on, [Line(mm, bond.currency, -fee), Line(funding, bond.currency, fee)],
                    memo=f"fee on {fill_id} ({LABEL})")
    conn.execute(
        "INSERT INTO mm_fills(id, run_id, step, order_id, instrument_id, currency, side, client, price, size, cash,"
        " bot_fair_dirty, true_fair_dirty, fee, txn_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fill_id, run_id, step, order_id, bond.id, bond.currency, side, client, plain(price), plain(size),
         plain(s.cash), plain(bot_fair), plain(true_fair), plain(fee), f"fill:{fill_id}", now.isoformat()))
    if order_id:
        row = conn.execute("SELECT size, filled FROM mm_orders WHERE id = ?", (order_id,)).fetchone()
        filled = D(row["filled"]) + size
        conn.execute("UPDATE mm_orders SET filled = ?, status = ?, updated_at = ? WHERE id = ?",
                     (plain(filled), "filled" if filled >= D(row["size"]) else "open", now.isoformat(), order_id))


def _unwind(conn, run_id, params, bonds, value, paths, on, start, mm, market, config_path) -> None:
    """Sell inventory above target to an outside market, at its bid less impact: slippage.
    Only ever reduces risk (nobody sells short), so it doesn't need the bot to be running;
    after a kill the caller decides (`run` won't, `unwind` will)."""
    seen, true = _seen_at(paths, params, params.steps - 1), {c: p[params.steps - 1] for c, p in paths.items()}
    now = start + timedelta(seconds=params.steps * params.step_seconds)
    for bond in bonds:
        qp = _sizes_and_fees(config_path, bond.id)
        excess = ledger.balance(conn, mm, bond.id) - qp.target_inventory
        if excess < qp.min_order:
            continue
        true_clean, accrued = value(bond, true[bond.currency])
        cost_bp = params.outside_half_spread_bp[bond.id] + params.impact_bp_per_million * excess / Decimal(1_000_000)
        price = (true_clean * (1 - cost_bp / BP_PER_UNIT) / qp.tick).to_integral_value(ROUND_FLOOR) * qp.tick
        seen_clean, _ = value(bond, seen[bond.currency])
        _fill(conn, run_id, params.steps, bond, "sell", price, excess, "unwind", seen_clean + accrued,
              true_clean + accrued, on, now, mm, market, config_path, fill_id=f"{run_id}:unwind:{bond.id}")


def _seen_at(paths, params, step) -> dict[str, Decimal]:
    """What the bot has seen of each curve by `step`."""
    return {c: p[max(step - params.lag_steps, 0)] for c, p in paths.items()}


def summarise(conn, run_id: str, bonds, value, paths, params, mm, unwind_note: str) -> dict:
    seen = _seen_at(paths, params, params.steps - 1)
    start = json.loads(conn.execute("SELECT start FROM mm_runs WHERE id = ?", (run_id,)).fetchone()["start"])
    rows = conn.execute("SELECT * FROM mm_fills WHERE run_id = ? ORDER BY step, id", (run_id,)).fetchall()
    fills = [pnl.FillForPnl(r["instrument_id"], r["currency"], r["side"], r["client"], D(r["size"]), D(r["cash"]),
                            D(r["bot_fair_dirty"]), D(r["fee"])) for r in rows]
    currencies = {b.id: b.currency for b in bonds}
    start_units = {i: D(v) for i, v in start["units"].items()}
    start_marks = {i: D(v) for i, v in start["marks"].items()}
    end_marks = {b.id: sum(value(b, seen[b.currency])) for b in bonds}
    held = ledger.balances(conn).get(mm, {})
    parts = pnl.attribute(fills, currencies, start_units, start_marks, end_marks)
    totals = pnl.total(currencies, {c: D(v) for c, v in start["cash"].items()}, held, start_units, held,
                       start_marks, end_marks)
    by_inst = {}
    for b in bonds:
        mine = [r for r in rows if r["instrument_id"] == b.id]
        by_inst[b.id] = {
            "fills": {c: sum(1 for r in mine if r["client"] == c) for c in CLIENTS},
            "bought": plain(sum((D(r["size"]) for r in mine if r["side"] == "buy"), ZERO)),
            "sold": plain(sum((D(r["size"]) for r in mine if r["side"] == "sell"), ZERO)),
            "inventory_start": plain(start_units[b.id]), "inventory_end": plain(held.get(b.id, ZERO)),
            "fair_start": plain(start_marks[b.id]), "fair_end": plain(end_marks[b.id]),
        }
    st = controls.state(conn)
    return {
        "label": LABEL,
        "profile": params.profile,
        "instruments": by_inst,
        "pnl": {c: {"spread": plain(p.spread), "start_mtm": plain(p.start_mtm), "traded_mtm": plain(p.traded_mtm),
                    "mtm": plain(p.mtm), "fees": plain(p.fees), "slippage": plain(p.slippage),
                    "total": plain(p.total), "total_from_ledger": plain(totals[c])}
                for c, p in parts.items()},
        "markouts": _markouts(rows, bonds, value, paths, params),
        "unwind": unwind_note,
        "bot_end": {"state": st["state"], "by": st["changed_by"], "reason": st["reason"]},
    }


def _markouts(rows, bonds, value, paths, params) -> dict:
    """Per currency and client type: fills, and the summed markout at each horizon.

    A fill's markout at h steps is what it's worth against the bot's own fair value h
    steps later: buying, size x fair / 100 - cash; selling, cash - size x fair / 100.
    Negative markouts on informed fills are adverse selection showing up as it happens.
    Unwinds are left out: they are the bot's own exit, not client flow."""
    by_id = {b.id: b for b in bonds}
    out: dict = {}
    for r in rows:
        if r["client"] == "unwind":
            continue
        bond = by_id[r["instrument_id"]]
        cell = out.setdefault(r["currency"], {}).setdefault(
            r["client"], {"fills": 0, **{f"{h}_steps": ZERO for h in MARKOUT_STEPS}})
        cell["fills"] += 1
        for h in MARKOUT_STEPS:
            later = _seen_at(paths, params, min(r["step"] + h, params.steps - 1))[bond.currency]
            worth = D(r["size"]) * sum(value(bond, later)) / HUNDRED
            cell[f"{h}_steps"] += worth - D(r["cash"]) if r["side"] == "buy" else D(r["cash"]) - worth
    return {c: {k: {m: v if m == "fills" else plain(v) for m, v in cell.items()} for k, cell in by.items()}
            for c, by in out.items()}


# ---------------------------------------------------------------- many seeds


def flatten(summary: dict) -> dict[str, float]:
    """One run's numbers, flat, for comparing many seeds: each P&L line per currency,
    fills and markouts per currency and client type, and whether the loss limit tripped."""
    out = {}
    for ccy, p in summary["pnl"].items():
        for line in ("spread", "start_mtm", "traded_mtm", "fees", "slippage", "total"):
            out[f"{ccy} {line}"] = float(p[line])
        for client in ("uninformed", "informed"):
            cell = summary["markouts"].get(ccy, {}).get(client, {})
            out[f"{ccy} {client} fills"] = float(cell.get("fills", 0))
            for h in MARKOUT_STEPS:
                out[f"{ccy} {client} markout {h} steps"] = float(cell.get(f"{h}_steps", 0))
    out["loss limit tripped"] = float(summary["bot_end"]["by"] == "risk check")
    return out


def mean_sd(rows: list[dict[str, float]]) -> dict[str, tuple[float, float]]:
    """Mean and sample standard deviation of each key over many runs (missing counts as 0)."""
    keys = sorted({k for r in rows for k in r})
    out = {}
    for k in keys:
        xs = [r.get(k, 0.0) for r in rows]
        out[k] = (statistics.fmean(xs), statistics.stdev(xs) if len(xs) > 1 else math.nan)
    return out
