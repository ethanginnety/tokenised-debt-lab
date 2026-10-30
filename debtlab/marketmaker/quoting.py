"""Deciding what to quote (M4 step 4): nothing unless everything checks out, and always say why.

For every instrument, in order:
1. A fund share is never quoted off its reported NAV.
2. The bot must be running, the ledger must pass its integrity check, and there must be a market maker.
3. The bond's curve must be official and fresh (`risk_increasing_allowed`).
4. The bond must value. Its lifecycle state sets what is allowed: *normal*; *restricted*
   (grace period: only the ask, at twice the spread and half the size); *halted*
   (defaulted, matured, outside its life): nothing.
5. The maximum inventory limit is read from the database (never the config copy).
6. Each side of the quote must pass the execution check, counting the sides already
   decided in this round as open and the instrument's own open orders as replaced.
   A side that fails is dropped with its reasons; insufficient cash or units show up
   here or in the quote model.
7. If the market maker holds a bond that couldn't be valued, its exposure is unknown,
   so no bid is made in that currency (or in any, if the bond isn't configured at all).

Every decision, a quote or not, is stored in `mm_quotes` with its reasons, its spread
components, data timestamps, inventory, and the limits in force. Nothing is placed
here: placing and filling orders is the simulated market's job (step 5).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from debtlab import ledger, lifecycle
from debtlab.instruments import FundShare, Instrument
from debtlab.marketdata.models import CurveSnapshot, risk_increasing_allowed
from debtlab.marketmaker import controls, execution
from debtlab.marketmaker.execution import Mark, Order
from debtlab.marketmaker.quotes import MarketInputs, Quote, inputs_from_valuation, load_quote_params, make_quote
from debtlab.money import D, ZERO, plain
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import value_bond

NAV_NEVER_QUOTED = "a fund share is never quoted off its reported NAV (a secondary price comes in M5)"


@dataclass(frozen=True)
class QuoteDecision:
    instrument_id: str
    status: str  # quoted | one_sided | none
    quoting_state: str  # normal | restricted | halted | never | unknown
    quote: Quote | None  # after dropping any side that failed a check
    reasons: tuple[str, ...]
    record_id: int


@dataclass(frozen=True)
class _Prepared:
    state: str
    inputs: MarketInputs | None
    mark: Mark | None
    reasons: tuple[str, ...]


def decide_quotes(conn: sqlite3.Connection, instruments: list[Instrument], curves: dict[str, CurveSnapshot],
                  on: date, now: datetime, max_curve_age_hours: int, config_path: Path,
                  daily_pnl: dict[str, Decimal]) -> list[QuoteDecision]:
    """Decide and record a quote (or none) for every instrument, valued on `on`."""
    blockers = _blockers(conn)
    mm = None if any(b.startswith("no market maker") for b in blockers) else execution.market_maker_id(conn)
    bonds = [i for i in instruments if isinstance(i, FixedRateBond)]
    held = {} if mm is None else ledger.balances(conn).get(mm, {})
    prepared = {i.id: _prepare(conn, i, curves, on, now, max_curve_age_hours,
                               held.get(i.id, ZERO)) for i in instruments}
    marks = {k: p.mark for k, p in prepared.items() if p.mark is not None}
    unknown = _unknown_exposure(held, bonds, marks)

    out, pending = [], []
    for inst in instruments:
        p = prepared[inst.id]
        reasons = list(p.reasons) if p.state == "never" else blockers + list(p.reasons)
        quote = None
        if not reasons:
            quote, side_reasons, orders = _quote_and_check(conn, inst, p, config_path, now, marks, daily_pnl,
                                                           tuple(pending), unknown)
            reasons += side_reasons
            pending += orders
        out.append(_record(conn, inst, p, quote, reasons, now))
    return out


def _blockers(conn: sqlite3.Connection) -> list[str]:
    """Reasons that stop every quote."""
    out = []
    st = controls.state(conn)
    if st["state"] != "running":
        out.append(f"bot is {st['state']} ({st['reason']})")
    if not conn.execute("SELECT 1 FROM parties WHERE role = 'market_maker'").fetchone():
        out.append("no market maker in this database (create a scenario database with --market-maker)")
    problems = ledger.check_integrity(conn)
    if problems:
        out.append(f"ledger integrity check failed: {'; '.join(problems[:3])}")
    return out


def _prepare(conn: sqlite3.Connection, inst: Instrument, curves: dict[str, CurveSnapshot], on: date,
             now: datetime, max_age: int, inventory: Decimal) -> _Prepared:
    if isinstance(inst, FundShare):
        return _Prepared("never", None, None, (NAV_NEVER_QUOTED,))
    snap = curves.get(inst.benchmark)
    if snap is None:
        return _Prepared("unknown", None, None, (f"no {inst.benchmark} curve",))
    reasons = []
    allowed, why = risk_increasing_allowed(snap, now, max_age)
    if not allowed:
        reasons.append(f"{inst.benchmark} curve: {why}")
    age = D(str(snap.age_hours(now)))
    try:
        sp = lifecycle.scenario_price(conn, inst, snap.curve, on)
        state = sp["quoting"]
        if state == "halted":
            return _Prepared(state, None, None, (*reasons, f"quoting halted: {sp.get('message', sp['status'])}"))
        if state == "restricted":
            # Priced flat on the distressed basis; DV01 by repricing on a curve 1 bp higher.
            clean = D(sp["clean"])
            dv01 = clean - D(lifecycle.scenario_price(conn, inst, snap.curve.shifted(1), on)["clean"])
            inputs = MarketInputs(inst.id, inst.currency, clean, ZERO, dv01, D(sp["spread_bp"]), None, None,
                                  snap.id, snap.observation_date, snap.fetched_at, age, on, inventory)
            return _Prepared(state, inputs, Mark(inst.currency, clean, ZERO, dv01, age), tuple(reasons))
        v = value_bond(inst, snap.curve, on)
    except Exception as exc:  # any failure to value means no quote, never a guessed price
        return _Prepared("unknown", None, None, (*reasons, f"valuation failed: {exc}"))
    inputs = inputs_from_valuation(v, inst, snap, inventory, now)
    return _Prepared(state, inputs, Mark(inst.currency, v.fair_dirty, v.accrued, v.risk.dv01, age), tuple(reasons))


def _unknown_exposure(held: dict[str, Decimal], bonds: list[FixedRateBond], marks: dict[str, Mark]) -> dict:
    """{currency or '*': reason}: where the market maker holds something it can't value."""
    by_id = {b.id: b for b in bonds}
    out = {}
    for asset, units in held.items():
        if ledger.is_cash(asset) or units == 0 or asset in marks:
            continue
        bond = by_id.get(asset)
        key = bond.currency if bond else "*"
        out.setdefault(key, f"exposure unknown: the market maker holds {asset} and it isn't valued")
    return out


def _quote_and_check(conn, inst, p: _Prepared, config_path, now, marks, daily_pnl, pending, unknown):
    try:
        cap = controls.limit(conn, "max_inventory", inst.id)
    except controls.ControlError as exc:
        return None, [str(exc)], []
    params = load_quote_params(config_path, inst.id, cap)
    quote = make_quote(p.inputs, params, now, restricted=p.state == "restricted")
    reasons, orders = list(quote.notes), []
    replacing = frozenset(o["id"] for o in execution.open_orders(conn) if o["instrument_id"] == inst.id)
    keep = {}
    for side, price, size in (("buy", quote.bid, quote.bid_size), ("sell", quote.ask, quote.ask_size)):
        if price is None:
            continue
        order = Order(f"{inst.id}:{side}:{now.isoformat()}", inst.id, side, price, size)
        broken = execution.check(conn, order, marks, daily_pnl, pending=pending + tuple(orders), replacing=replacing)
        if side == "buy" and (why := unknown.get(inst.currency) or unknown.get("*")):
            broken.append(why)
        if broken:
            reasons += [f"no {'bid' if side == 'buy' else 'ask'}: {r}" for r in broken]
        else:
            orders.append(order)
            keep[side] = True
    if not keep.get("buy") and quote.bid is not None:
        quote = _drop(quote, "bid")
    if not keep.get("sell") and quote.ask is not None:
        quote = _drop(quote, "ask")
    return quote, reasons, orders


def _drop(quote: Quote, side: str) -> Quote:
    return replace(quote, **{side: None, f"{side}_size": ZERO})


def _record(conn: sqlite3.Connection, inst: Instrument, p: _Prepared, quote: Quote | None,
            reasons: list[str], now: datetime) -> QuoteDecision:
    two_sided = quote is not None and quote.bid is not None and quote.ask is not None
    one_side = quote is not None and (quote.bid is not None or quote.ask is not None)
    status = "quoted" if two_sided else "one_sided" if one_side else "none"
    if status == "none":
        quote = None
    ccy = getattr(inst, "currency", None)
    in_force = {f"{n}:{s}": plain(v) for (n, s), v in controls.limits(conn).items() if s in (inst.id, ccy, controls.ALL)}
    detail = {"limits_in_force": in_force}
    if p.inputs is not None:
        detail["data"] = {"curve_id": p.inputs.curve_id, "curve_observation_date": p.inputs.curve_observation_date.isoformat(),
                          "curve_fetched_at": p.inputs.curve_fetched_at.isoformat(),
                          "curve_age_hours": plain(p.inputs.curve_age_hours), "settle": p.inputs.settle.isoformat()}
        detail["inventory"] = plain(p.inputs.inventory)
    if quote is not None:
        size = max(quote.bid_size, quote.ask_size)
        detail |= {
            "imbalance": plain(quote.imbalance), "skew": plain(quote.skew), "reservation": plain(quote.reservation),
            "half_spread": plain(quote.half_spread), "bid_size": plain(quote.bid_size), "ask_size": plain(quote.ask_size),
            "target_inventory": plain(quote.target_inventory), "max_inventory": plain(quote.max_inventory),
            "spread": None if quote.spread is None else plain(quote.spread),
            "spread_bp": None if quote.spread_bp is None else plain(quote.spread_bp),
            "spread_cash": None if quote.spread is None else plain(quote.spread_cash(size)),
            "components": [{"name": c.name, "per_100": plain(c.per_100), "bp": plain(c.bp), "reason": c.reason}
                           for c in quote.components],
            "long_end_range": None if quote.long_end_range is None else [plain(x) for x in quote.long_end_range],
        }
    fair = p.inputs.fair_clean if p.inputs is not None else None
    cur = conn.execute(
        "INSERT INTO mm_quotes(instrument_id, quoted_at, status, quoting_state, fair_clean, bid, ask, reasons, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (inst.id, now.isoformat(), status, p.state, None if fair is None else plain(fair),
         None if quote is None or quote.bid is None else plain(quote.bid),
         None if quote is None or quote.ask is None else plain(quote.ask),
         json.dumps(reasons), json.dumps(detail, default=str)),
    )
    return QuoteDecision(inst.id, status, p.state, quote, tuple(reasons), cur.lastrowid)


def recent_quotes(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM mm_quotes ORDER BY id DESC LIMIT ?", (limit,))
    return [{**dict(r), "reasons": json.loads(r["reasons"]), "detail": json.loads(r["detail"])} for r in rows]

