"""The quote model (M4 step 2): components, skew, tick rounding, sizes, and real-curve quotes."""

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from debtlab.instruments import load_instruments
from debtlab.marketdata import adapters
from debtlab.marketmaker.quotes import (
    MarketInputs, QuoteParams, inputs_from_valuation, load_quote_params, make_quote,
)
from debtlab.money import D, q
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import value_bond

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
CONFIG = ROOT / "config" / "marketmaker.json"
BONDS = {b.id: b for b in load_instruments(ROOT / "config" / "instruments.json") if isinstance(b, FixedRateBond)}
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

# Round numbers so every figure can be checked by hand (TEST values, not a real bond).
PARAMS = QuoteParams(
    tick=D("0.01"), min_order=D(10_000), size_step=D(1_000), quote_size=D(100_000),
    target_inventory=D(1_000_000), max_inventory=D(2_000_000),
    base_bp=D(1), intraday_move_bp=D(1), credit_factor=D("0.005"), data_age_bp_per_day=D("0.5"),
    inventory_widen_bp=D(2), skew_bp=D(3), illiquidity_bp=D(2), fee_bp=D("0.5"), long_end_weight=D("0.5"),
)
INPUTS = MarketInputs(
    "TEST", "GBP", fair_clean=D(100), accrued=D(1), dv01=D("0.1"), credit_spread_bp=D(100),
    long_end_band=(D(101), D(99)), long_end_range=(D(98), D(104)),
    curve_id="GBP:test:2026-10-28", curve_observation_date=date(2026, 10, 28),
    curve_fetched_at=datetime(2026, 10, 29, 9, tzinfo=timezone.utc), curve_age_hours=D(48),
    settle=date(2026, 10, 30), inventory=D(1_000_000),
)


def parts(quote):
    return {c.name: c.per_100 for c in quote.components}


def test_hand_example_at_target_inventory():
    quote = make_quote(INPUTS, PARAMS, NOW)
    assert parts(quote) == {
        "base": D("0.01"),                 # 1 bp of 100
        "duration": D("0.1"),              # 1 bp x DV01 0.1
        "credit": D("0.05"),               # 0.5% of 100 bp = 0.5 bp x 0.1
        "data age": D("0.1"),              # 0.5 bp a day x 2 days x 0.1
        "inventory": D(0),                 # at target
        "illiquidity": D("0.02"),          # 2 bp of 100
        "fees": D("0.00505"),              # 0.5 bp of the 101 paid (clean + accrued)
        "long-end model risk": D("0.5"),   # 0.5 x (101 - 99) / 2
    }
    assert quote.half_spread == D("0.78505") and quote.skew == 0
    # 100 - 0.78505 = 99.21495 rounds down to 99.21; 100.78505 rounds up to 100.79.
    assert (quote.bid, quote.ask) == (D("99.21"), D("100.79"))
    assert (quote.bid_size, quote.ask_size) == (D(100_000), D(100_000))
    assert quote.spread == D("1.58") and quote.spread_bp == D(158)
    assert quote.spread_cash(D(100_000)) == D(1580)  # a round trip on £100,000 earns £1,580
    assert quote.components[3].bp == D(10)  # each component also in bp of fair value
    assert all(c.reason for c in quote.components)


def test_quote_records_its_data_and_context():
    quote = make_quote(INPUTS, PARAMS, NOW)
    assert quote.quoted_at == NOW and quote.inventory == D(1_000_000)
    assert quote.data == {"curve_id": "GBP:test:2026-10-28", "curve_observation_date": "2026-10-28",
                          "curve_fetched_at": "2026-10-29T09:00:00+00:00", "curve_age_hours": D(48),
                          "settle": "2026-10-30"}
    assert quote.long_end_range == (D(98), D(104))  # recorded as context, not priced in


def test_long_inventory_skews_down_and_widens():
    # 1.5m held against a 1m target and 2m limit: halfway to the limit.
    quote = make_quote(replace(INPUTS, inventory=D(1_500_000)), PARAMS, NOW)
    assert quote.imbalance == D("0.5")
    assert quote.skew == D("0.15")  # 3 bp x 0.5 x DV01 0.1
    assert quote.reservation == D("99.85")
    assert parts(quote)["inventory"] == D("0.1")  # 2 bp x 0.5 x 0.1
    # 99.85 -/+ 0.88505 -> 98.96495 down to 98.96, 100.73505 up to 100.74
    assert (quote.bid, quote.ask) == (D("98.96"), D("100.74"))


def test_short_of_target_skews_up():
    quote = make_quote(replace(INPUTS, inventory=D(250_000)), PARAMS, NOW)
    assert quote.imbalance == D("-0.75") and quote.reservation == D("100.225")
    assert quote.bid > make_quote(INPUTS, PARAMS, NOW).bid  # keener to buy


def test_sides_drop_out_at_the_inventory_limits():
    full = make_quote(replace(INPUTS, inventory=D(2_000_000)), PARAMS, NOW)
    assert (full.bid, full.bid_size, full.ask_size) == (None, 0, D(100_000))
    assert full.spread is None and full.notes[0].startswith("no bid")
    empty = make_quote(replace(INPUTS, inventory=D(0)), PARAMS, NOW)
    assert (empty.ask, empty.ask_size, empty.bid_size) == (None, 0, D(100_000))
    assert "no short sales" in empty.notes[0]


def test_sizes_respect_step_and_minimum():
    def sizes(inventory):
        quote = make_quote(replace(INPUTS, inventory=D(inventory)), PARAMS, NOW)
        return quote.bid_size, quote.ask_size
    assert sizes("54321.5") == (D(100_000), D(54_000))  # rounded down to 1,000s
    assert sizes(5_000) == (D(100_000), 0)  # 5,000 is below the 10,000 minimum
    assert sizes(1_995_500) == (0, D(100_000))  # 4,500 of room: no bid
    assert sizes(1_950_000) == (D(50_000), D(100_000))


def test_ticks_round_outward():
    quote = make_quote(replace(INPUTS, fair_clean=D("100.004")), PARAMS, NOW)
    assert quote.bid == D("99.21") and quote.ask == D("100.79")  # 99.21895 down, 100.78905 up
    coarse = make_quote(INPUTS, replace(PARAMS, tick=D("0.25")), NOW)
    assert (coarse.bid, coarse.ask) == (D("99.00"), D("101.00"))


@pytest.mark.parametrize("change", [
    dict(curve_age_hours=D(96)), dict(dv01=D("0.2")), dict(credit_spread_bp=D(500)),
    dict(long_end_band=(D(102), D(98))), dict(inventory=D(1_800_000)),
])
def test_more_risk_means_a_wider_spread(change):
    base = make_quote(INPUTS, PARAMS, NOW).half_spread
    assert make_quote(replace(INPUTS, **change), PARAMS, NOW).half_spread > base


def test_no_long_end_component_when_the_curve_covers_the_bond():
    quote = make_quote(replace(INPUTS, long_end_band=None, long_end_range=None), PARAMS, NOW)
    assert parts(quote)["long-end model risk"] == 0
    assert quote.half_spread == D("0.28505")


def test_bid_never_at_or_below_zero():
    quote = make_quote(replace(INPUTS, fair_clean=D("0.5"), accrued=D(0)), PARAMS, NOW)
    assert quote.bid is None and quote.ask is not None and "zero price" in quote.notes[-1]


# ---------------------------------------------------------------- the real 29 Oct curves


@pytest.fixture(scope="module")
def real():
    gbp = adapters.parse_boe_zip((REAL / "boe_2026-10-30.zip").read_bytes(), NOW)
    usd = adapters.to_zero_snapshot(adapters.parse_treasury_csv(
        (REAL / "treasury_2026-10-30.csv").read_text(encoding="utf-8"), NOW))
    out = {}
    for bond_id, snap in (("CENTURY-GBP-2126", gbp), ("SHORT-USD-2029", usd)):
        bond = BONDS[bond_id]
        v = value_bond(bond, snap.curve, NOW.date())
        params = load_quote_params(CONFIG, bond_id)
        out[bond_id] = make_quote(inputs_from_valuation(v, bond, snap, params.target_inventory, NOW), params, NOW)
    return out


def test_century_quote_on_the_real_gilt_curve(real):
    quote = real["CENTURY-GBP-2126"]
    assert q(quote.fair_clean, 4) == D("90.6201")  # as hand-checked in M2
    # Approved add-on: 0.5 x (91.2748 - 90.0543) / 2 = 0.305 per 100, about 34 bp of the price.
    assert q(parts(quote)["long-end model risk"], 3) == D("0.305")
    assert quote.long_end_range == (min(quote.long_end_range), max(quote.long_end_range))
    assert q(quote.long_end_range[0], 2) == D("89.56") and q(quote.long_end_range[1], 2) == D("94.11")
    assert quote.bid < quote.fair_clean < quote.ask and quote.data["curve_age_hours"] == D(36)


def test_short_bond_has_no_long_end_risk_and_a_tighter_spread(real):
    short, century = real["SHORT-USD-2029"], real["CENTURY-GBP-2126"]
    assert parts(short)["long-end model risk"] == 0
    assert short.spread_bp * 5 < century.spread_bp  # far less duration, credit and model risk


def test_config_overrides_per_instrument():
    assert load_quote_params(CONFIG, "CENTURY-GBP-2126").illiquidity_bp == D(10)
    assert load_quote_params(CONFIG, "SHORT-USD-2029").illiquidity_bp == D(2)
    assert load_quote_params(CONFIG, "SHORT-USD-2029").max_inventory == D(2_000_000)
