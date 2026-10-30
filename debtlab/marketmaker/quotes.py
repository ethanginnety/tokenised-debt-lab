"""The quote model (M4 step 2): pure functions from a valuation to a two-way quote.

A quote starts at fair value (clean, per 100 face) and moves against inventory:
when the bot holds more than its target, both prices go down to attract buyers
(the *skew*). A half-spread is then added either side. It is a sum of named
components, each kept with its size and the reason for it, so every quote can
be explained line by line. Most components are a yield move in bp times the
bond's DV01, because what a market maker fears is the price moving before it can
react, and DV01 turns a yield move into a price move: the same 1 bp costs a
century bond about six times what it costs a three-year bond.

Prices are rounded outward to the tick (bid down, ask up), so rounding never
narrows the spread. A side is left out when the bot can't honour it: no ask
without inventory to sell (nobody sells short), no bid at maximum inventory.
Whether to quote at all (data, limits, the bond's state) is decided before this
(M4 steps 3 and 4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from pathlib import Path

from debtlab.marketdata.models import CurveSnapshot
from debtlab.money import D, HUNDRED, ONE, ZERO
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import LONG_END_RULES, Valuation

BP_PER_UNIT = Decimal(10_000)
HOURS_PER_DAY = Decimal(24)
# The long-end band: the sensitivity cases that move the long-run forward by -50 and +50 bp.
BAND_LOW, BAND_HIGH = (next(name for name, _, _, shift in LONG_END_RULES if shift == s) for s in (-50, 50))


@dataclass(frozen=True)
class QuoteParams:
    """Per-instrument settings (config/marketmaker.json). bp of yield unless named 'of price'."""
    tick: Decimal  # per 100 face
    min_order: Decimal  # face
    size_step: Decimal  # face
    quote_size: Decimal  # face, each side
    target_inventory: Decimal  # face the bot aims to hold
    max_inventory: Decimal  # face
    base_bp: Decimal  # of price: the floor cost of quoting at all
    intraday_move_bp: Decimal  # yield move assumed before the bot can react
    credit_factor: Decimal  # share of the credit spread taken as a possible yield move
    data_age_bp_per_day: Decimal  # yield move assumed per day of curve age
    inventory_widen_bp: Decimal  # extra yield move at full inventory imbalance
    skew_bp: Decimal  # price shift, as a yield move, at full imbalance
    illiquidity_bp: Decimal  # of price: how hard the bond is to trade out of
    fee_bp: Decimal  # of cash paid or received, per fill
    long_end_weight: Decimal  # share of the long-end model-risk band added


@dataclass(frozen=True)
class MarketInputs:
    """Everything a quote depends on, as of one moment."""
    instrument_id: str
    currency: str
    fair_clean: Decimal  # per 100
    accrued: Decimal  # per 100
    dv01: Decimal  # price change per 100 face for 1 bp of yield
    credit_spread_bp: Decimal
    long_end_band: tuple[Decimal, Decimal] | None  # clean at long-run forward -50 / +50 bp; None if the curve covers the bond
    long_end_range: tuple[Decimal, Decimal] | None  # lowest and highest clean price over every long-end rule (context only)
    curve_id: str
    curve_observation_date: date
    curve_fetched_at: datetime
    curve_age_hours: Decimal
    settle: date
    inventory: Decimal  # face held by the market maker


@dataclass(frozen=True)
class Component:
    name: str
    per_100: Decimal  # added to the half-spread, per 100 face
    bp: Decimal  # the same, in bp of fair value
    reason: str


@dataclass(frozen=True)
class Quote:
    instrument_id: str
    currency: str
    quoted_at: datetime
    fair_clean: Decimal
    imbalance: Decimal  # -1 (no inventory) .. 0 (at target) .. +1 (at maximum)
    skew: Decimal  # subtracted from fair value, per 100
    reservation: Decimal  # fair value after skew: the centre of the quote
    components: tuple[Component, ...]
    half_spread: Decimal  # sum of the components, per 100
    bid: Decimal | None  # clean per 100; None when that side isn't quoted
    ask: Decimal | None
    bid_size: Decimal  # face; 0 when that side isn't quoted
    ask_size: Decimal
    inventory: Decimal
    target_inventory: Decimal
    max_inventory: Decimal
    data: dict  # curve id, observation date, fetch time, age; settlement date
    long_end_range: tuple[Decimal, Decimal] | None  # context only, not used in the price
    notes: tuple[str, ...] = field(default=())  # why a side is missing

    @property
    def spread(self) -> Decimal | None:
        """Ask minus bid, per 100 face (None if one side is missing)."""
        return None if self.bid is None or self.ask is None else self.ask - self.bid

    @property
    def spread_bp(self) -> Decimal | None:
        return None if self.spread is None else self.spread / self.fair_clean * BP_PER_UNIT

    def spread_cash(self, face: Decimal) -> Decimal | None:
        """What a round trip at bid and ask earns on `face`, in currency."""
        return None if self.spread is None else self.spread * face / HUNDRED


def make_quote(m: MarketInputs, p: QuoteParams, now: datetime) -> Quote:
    imbalance = _imbalance(m.inventory, p.target_inventory, p.max_inventory)
    skew = m.dv01 * p.skew_bp * imbalance
    reservation = m.fair_clean - skew
    components = _components(m, p, imbalance)
    half = sum((c.per_100 for c in components), ZERO)

    bid_size, ask_size, notes = _sizes(m.inventory, p)
    bid = _to_tick(reservation - half, p.tick, ROUND_FLOOR) if bid_size else None
    ask = _to_tick(reservation + half, p.tick, ROUND_CEILING) if ask_size else None
    if bid is not None and bid <= 0:
        bid, bid_size, notes = None, ZERO, notes + ("no bid: the spread reaches zero price",)
    return Quote(
        m.instrument_id, m.currency, now, m.fair_clean, imbalance, skew, reservation, components, half,
        bid, ask, bid_size, ask_size, m.inventory, p.target_inventory, p.max_inventory,
        {"curve_id": m.curve_id, "curve_observation_date": m.curve_observation_date.isoformat(),
         "curve_fetched_at": m.curve_fetched_at.isoformat(), "curve_age_hours": m.curve_age_hours,
         "settle": m.settle.isoformat()},
        m.long_end_range, notes,
    )


def _components(m: MarketInputs, p: QuoteParams, imbalance: Decimal) -> tuple[Component, ...]:
    def c(name: str, per_100: Decimal, reason: str) -> Component:
        return Component(name, per_100, per_100 / m.fair_clean * BP_PER_UNIT, reason)

    age_days = m.curve_age_hours / HOURS_PER_DAY
    credit_move = m.credit_spread_bp * p.credit_factor
    out = [
        c("base", m.fair_clean * p.base_bp / BP_PER_UNIT, f"floor cost of quoting: {p.base_bp} bp of price"),
        c("duration", m.dv01 * p.intraday_move_bp,
          f"{p.intraday_move_bp} bp yield move before the bot can react x DV01 {m.dv01:.5f}"),
        c("credit", m.dv01 * credit_move,
          f"{p.credit_factor * 100}% of the {m.credit_spread_bp} bp credit spread = {credit_move} bp x DV01"),
        c("data age", m.dv01 * p.data_age_bp_per_day * age_days,
          f"curve {m.curve_age_hours:.1f}h old: {p.data_age_bp_per_day} bp a day x {age_days:.2f} days x DV01"),
        c("inventory", m.dv01 * p.inventory_widen_bp * abs(imbalance),
          f"inventory {m.inventory:,f} vs target {p.target_inventory:,f}: "
          f"{p.inventory_widen_bp} bp x {abs(imbalance):.2f} of the way to a limit x DV01"),
        c("illiquidity", m.fair_clean * p.illiquidity_bp / BP_PER_UNIT,
          f"hard to trade out of: {p.illiquidity_bp} bp of price"),
        c("fees", (m.fair_clean + m.accrued) * p.fee_bp / BP_PER_UNIT,
          f"{p.fee_bp} bp of the cash paid per fill"),
    ]
    if m.long_end_band is None:
        out.append(c("long-end model risk", ZERO, "none: the curve covers every payment"))
    else:
        low, high = m.long_end_band
        out.append(c("long-end model risk", p.long_end_weight * abs(low - high) / 2,
                     f"{p.long_end_weight} x half the gap between {low:.4f} and {high:.4f} "
                     "(long-run forward -50 / +50 bp beyond the curve)"))
    return tuple(out)


def _imbalance(inventory: Decimal, target: Decimal, maximum: Decimal) -> Decimal:
    """How far inventory is from target, towards the limit on that side: -1 at zero, +1 at maximum."""
    room = maximum - target if inventory >= target else target
    return max(-ONE, min(ONE, (inventory - target) / room)) if room else ZERO


def _sizes(inventory: Decimal, p: QuoteParams) -> tuple[Decimal, Decimal, tuple[str, ...]]:
    def size(available: Decimal) -> Decimal:
        s = (min(p.quote_size, max(available, ZERO)) / p.size_step).to_integral_value(ROUND_DOWN) * p.size_step
        return s if s >= p.min_order else ZERO

    bid_size, ask_size = size(p.max_inventory - inventory), size(inventory)
    notes = []
    if not bid_size:
        notes.append(f"no bid: room to maximum inventory ({p.max_inventory - inventory:,f}) "
                     f"below the minimum order {p.min_order:,f}")
    if not ask_size:
        notes.append(f"no ask: inventory {inventory:,f} below the minimum order {p.min_order:,f} (no short sales)")
    return bid_size, ask_size, tuple(notes)


def _to_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).to_integral_value(rounding) * tick


# ---------------------------------------------------------------- from the app's objects


def inputs_from_valuation(v: Valuation, bond: FixedRateBond, snapshot: CurveSnapshot,
                          inventory: Decimal, now: datetime) -> MarketInputs:
    cases = {c.name: c.clean_price for c in v.long_end}
    band = (cases[BAND_LOW], cases[BAND_HIGH]) if BAND_LOW in cases and BAND_HIGH in cases else None
    prices = list(cases.values())
    return MarketInputs(
        bond.id, bond.currency, v.fair_clean, v.accrued, v.risk.dv01, bond.credit_spread * BP_PER_UNIT,
        band, (min(prices), max(prices)) if prices else None,
        snapshot.id, snapshot.observation_date, snapshot.fetched_at, D(str(snapshot.age_hours(now))),
        v.settle, inventory,
    )


def load_quote_params(path: Path, instrument_id: str) -> QuoteParams:
    """Defaults, overridden per instrument; target and maximum inventory from the book and limits."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    q = {**data["quoting"]["defaults"], **data["quoting"].get(instrument_id, {})}
    q = {k: v for k, v in q.items() if not k.startswith("_")}
    return QuoteParams(
        **{k: D(v) for k, v in q.items()},
        target_inventory=D(data["units"][instrument_id]["market_maker"]),
        max_inventory=D(data["limits"]["max_inventory"][instrument_id]),
    )
