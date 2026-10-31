"""Quote nothing when anything is invalid, and record every decision (M4 step 4)."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from debtlab import ledger, lifecycle
from debtlab.audit.db import connect, migrate
from debtlab.instruments import FundShare, load_instruments
from debtlab.marketdata import adapters
from debtlab.marketmaker import controls
from debtlab.marketmaker.book import load_book_config, seed_book
from debtlab.marketmaker.execution import Mark, Order, place_order
from debtlab.marketmaker.quotes import inputs_from_valuation, load_quote_params, make_quote
from debtlab.marketmaker.quoting import NAV_NEVER_QUOTED, decide_quotes, recent_quotes
from debtlab.money import D
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import value_bond
from debtlab.scenario import create_scenario_db
from test_marketdata_audit_api import settings

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
CONFIG = ROOT / "config" / "marketmaker.json"
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.json")
BONDS = [i for i in INSTRUMENTS if isinstance(i, FixedRateBond)]
CENTURY = next(b for b in BONDS if b.id == "CENTURY-GBP-2126")
SHORT = next(b for b in BONDS if b.id == "SHORT-USD-2029")
FUND = next(i for i in INSTRUMENTS if isinstance(i, FundShare))
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
GBP = adapters.parse_boe_zip((REAL / "boe_2026-10-30.zip").read_bytes(), NOW)
USD = adapters.to_zero_snapshot(adapters.parse_treasury_csv(
    (REAL / "treasury_2026-10-30.csv").read_text(encoding="utf-8"), NOW))
CURVES = {"GBP": GBP, "USD": USD}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "main.sqlite3")
    migrate(c)
    ledger.seed(c, ledger.load_holders_config(ROOT / "config" / "holders.json"), INSTRUMENTS)
    seed_book(c, load_book_config(CONFIG), INSTRUMENTS)
    controls.seed_limits(c, CONFIG)
    controls.resume(c, "test", "quote", NOW)
    yield c
    c.close()


def decide(conn, curves=CURVES, on=TODAY, now=NOW, instruments=INSTRUMENTS):
    return {d.instrument_id: d for d in decide_quotes(conn, instruments, curves, on, now, 96, CONFIG)}


def test_both_bonds_quoted_and_the_fund_never(conn):
    d = decide(conn)
    century, short, fund = d[CENTURY.id], d[SHORT.id], d[FUND.id]
    assert (century.status, short.status) == ("quoted", "quoted") and century.reasons == ()
    # The same quote the pure model gives on the same inputs, with the database's 2m limit.
    params = load_quote_params(CONFIG, CENTURY.id, D(2_000_000))
    v = value_bond(CENTURY, GBP.curve, TODAY)
    expected = make_quote(inputs_from_valuation(v, CENTURY, GBP, D(1_000_000), NOW), params, NOW)
    assert (century.quote.bid, century.quote.ask) == (expected.bid, expected.ask) == (D("89.90"), D("91.34"))
    assert (fund.status, fund.quoting_state, fund.reasons) == ("none", "never", (NAV_NEVER_QUOTED,))


def test_every_decision_is_recorded_in_full(conn):
    decide(conn)
    rows = {r["instrument_id"]: r for r in recent_quotes(conn)}
    assert set(rows) == {CENTURY.id, SHORT.id, FUND.id}
    r = rows[CENTURY.id]
    assert (r["status"], r["quoting_state"], r["bid"], r["ask"]) == ("quoted", "normal", "89.9", "91.34")
    det = r["detail"]
    assert det["data"]["curve_id"] == GBP.id and det["data"]["curve_age_hours"] == "36"
    assert det["data"]["curve_observation_date"] == "2026-10-29" and det["inventory"] == "1000000"
    assert det["spread"] == "1.44" and det["spread_cash"] == "1440" and float(det["spread_bp"]) == pytest.approx(158.9, abs=0.05)
    assert [c["name"] for c in det["components"]][-1] == "long-end model risk"
    assert all(c["reason"] for c in det["components"])
    assert det["limits_in_force"]["max_inventory:CENTURY-GBP-2126"] == "2000000"
    assert det["limits_in_force"]["max_daily_loss:GBP"] == "20000"
    assert "max_daily_loss:USD" not in det["limits_in_force"]  # only what applies to this bond
    assert rows[FUND.id]["reasons"] == [NAV_NEVER_QUOTED]


def test_maximum_inventory_comes_from_the_database(conn):
    controls.set_limit(conn, "max_inventory", CENTURY.id, D(1_050_000), "Ethan", "tighter for the test")
    q = decide(conn)[CENTURY.id].quote
    assert q.max_inventory == D(1_050_000)  # not the config's 2,000,000
    assert q.bid_size == D(50_000)  # only 50,000 of room
    assert q.imbalance == 0 and q.skew == 0
    ledger.trade(conn, "buy-in", CENTURY, "market-maker", "market", D(50_000), D(90), TODAY)
    d = decide(conn)[CENTURY.id]
    assert d.status == "one_sided" and d.quote.bid is None and d.quote.imbalance == 1
    assert d.reasons[0].startswith("no bid: room to maximum inventory (0)")


def test_nothing_while_the_bot_is_not_running(conn):
    controls.pause(conn, "Ethan", "lunch")
    d = decide(conn)
    assert {k: v.status for k, v in d.items()} == dict.fromkeys(d, "none")
    assert d[CENTURY.id].reasons == ("bot is paused (lunch)",)
    controls.kill(conn, "Ethan", "stop")
    assert decide(conn)[SHORT.id].reasons == ("bot is killed (stop)",)


@pytest.mark.parametrize("curves, now, reason", [
    ({"USD": USD}, NOW, "no GBP curve"),
    ({**CURVES, "GBP": replace(GBP, kind="illustrative")}, NOW, "GBP curve: illustrative data can't drive risk-taking"),
    (CURVES, NOW + timedelta(days=4), "GBP curve: curve is stale (132h old)"),
])
def test_nothing_on_missing_illustrative_or_stale_data(conn, curves, now, reason):
    d = decide(conn, curves=curves, now=now)[CENTURY.id]
    assert d.status == "none" and reason in d.reasons


def test_nothing_when_valuation_fails(conn):
    wrong_ccy = {"GBP": GBP, "USD": replace(USD, curve=GBP.curve)}  # a GBP curve where USD belongs
    d = decide(conn, curves=wrong_ccy)[SHORT.id]
    assert d.status == "none" and d.reasons[-1].startswith("valuation failed: SHORT-USD-2029 needs a USD curve")


def test_unvalued_holding_blocks_bids_in_its_currency(conn):
    # USD curve missing: the short bond can't be valued, but the century bond (GBP) still quotes.
    d = decide(conn, curves={"GBP": GBP})
    assert d[SHORT.id].status == "none" and d[CENTURY.id].status == "quoted"
    # A holding that isn't configured at all: its currency is unknown, so no bids anywhere.
    d = decide(conn, instruments=[CENTURY])
    assert d[CENTURY.id].status == "one_sided" and d[CENTURY.id].quote.ask is not None
    assert "no bid: exposure unknown: the market maker holds SHORT-USD-2029 and it isn't valued" in d[CENTURY.id].reasons


def test_nothing_when_the_ledger_fails_its_integrity_check(conn):
    conn.execute("INSERT INTO ledger_txns(id, kind, effective_date, fingerprint, created_at)"
                 " VALUES ('bad', 'test', '2026-10-30', 'x', 'x')")
    conn.execute("INSERT INTO ledger_entries(txn_id, owner, asset, amount) VALUES ('bad', 'alder', 'USD', '1')")
    d = decide(conn)[CENTURY.id]
    assert d.status == "none" and d.reasons[0].startswith("ledger integrity check failed: bad does not balance in USD")


def test_not_enough_cash_drops_the_bid(conn):
    controls.set_limit(conn, "min_cash_reserve", "GBP", D(1_450_000), "Ethan", "test")
    d = decide(conn)[CENTURY.id]
    assert d.status == "one_sided" and d.quote.bid is None and d.quote.ask == D("91.34")
    assert d.reasons[0].startswith("no bid: GBP cash would fall to")


def test_no_units_drops_the_ask(conn):
    ledger.trade(conn, "sell-out", CENTURY, "market", "market-maker", D(1_000_000), D(90), TODAY)
    d = decide(conn)[CENTURY.id]
    assert d.status == "one_sided" and d.quote.ask is None
    assert "no short sales" in d.reasons[0]


def test_sides_decided_in_the_same_round_count_as_open(conn):
    controls.set_limit(conn, "max_open_orders", "all", D(3), "Ethan", "test")
    d = decide(conn)
    assert d[CENTURY.id].status == "quoted"  # two orders
    assert d[SHORT.id].status == "one_sided"  # its bid is the third, its ask would be a fourth
    assert d[SHORT.id].reasons == ("no ask: 3 orders already open; the maximum is 3",)


def test_own_open_orders_are_replaced_not_counted(conn):
    marks = {CENTURY.id: Mark("GBP", D(94), D("3.5"), D("0.137"), D(36))}
    for side, price in (("buy", "89.80"), ("sell", "91.40")):
        assert place_order(conn, Order(f"old-{side}", CENTURY.id, side, D(price), D(100_000)), marks, NOW).allowed
    assert decide(conn)[CENTURY.id].status == "quoted"  # 2 old + 2 new would be 4, still fine; the short:
    controls.set_limit(conn, "max_open_orders", "all", D(2), "Ethan", "test")
    d = decide(conn)
    assert d[CENTURY.id].status == "quoted"  # replaces its own two
    assert d[SHORT.id].status == "none"  # the century's two count against it


def test_daily_loss_in_one_currency_pauses_everything(conn):
    # Decision 3: a breach in either currency pauses the whole bot and cancels every order.
    assert decide(conn)[CENTURY.id].status == "quoted"  # opens the day at today's curves
    gilts_up = {**CURVES, "GBP": replace(GBP, curve=GBP.curve.shifted(D(20)))}  # gilt yields +20 bp
    d = decide(conn, curves=gilts_up)  # the 1m century loses more than 20,000 on the day, from the ledger
    assert d[CENTURY.id].status == d[SHORT.id].status == "none"
    assert d[SHORT.id].reasons[0].startswith("bot is paused (daily loss ")
    assert d[SHORT.id].reasons[0].endswith("GBP has reached the maximum 20,000)")
    assert controls.state(conn)["changed_by"] == "risk check"


def test_halted_after_maturity(conn):
    d = decide(conn, on=date(2029, 3, 1))[SHORT.id]
    assert (d.status, d.quoting_state) == ("none", "halted")
    assert d.reasons == ("quoting halted: outside the bond's life",)


# ---------------------------------------------------------------- restricted and halted in a scenario


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / "scenario.sqlite3"
    create_scenario_db(path, settings(tmp_path), NOW, market_maker=True)
    c = connect(path)
    controls.resume(c, "test", "quote", NOW)
    lifecycle.pay_due(c, BONDS)
    lifecycle.trigger_default(c, SHORT)
    yield c
    c.close()


def test_grace_period_quotes_only_the_ask_wider_and_smaller(scenario):
    on = date(2027, 2, 22)
    lifecycle.advance_simulation(scenario, on)
    lifecycle.pay_due(scenario, BONDS)
    later = NOW.replace(year=2027, month=2, day=22)
    curves = {k: replace(v, observation_date=on - timedelta(days=1)) for k, v in CURVES.items()}  # fresh then
    d = decide(scenario, curves=curves, on=on, now=later)[SHORT.id]
    assert (d.status, d.quoting_state) == ("one_sided", "restricted")
    q = d.quote
    sp = lifecycle.scenario_price(scenario, SHORT, USD.curve, on)
    assert q.fair_clean == D(sp["clean"]) and q.bid is None and q.ask_size == D(50_000)
    parts = {c.name: c.per_100 for c in q.components}
    assert parts["restricted"] == sum(v for k, v in parts.items() if k != "restricted")
    assert parts["long-end model risk"] == 0 and q.ask > q.fair_clean
    assert "grace period" in d.reasons[0]
    row = next(r for r in recent_quotes(scenario) if r["instrument_id"] == SHORT.id)
    assert (row["status"], row["quoting_state"], row["bid"]) == ("one_sided", "restricted", None)
    assert row["detail"]["ask_size"] == "50000" and row["detail"]["data"]["settle"] == "2027-02-22"


def test_defaulted_bond_is_halted(scenario):
    on = date(2027, 3, 22)
    lifecycle.advance_simulation(scenario, on)
    lifecycle.pay_due(scenario, BONDS)
    later = NOW.replace(year=2027, month=3, day=22)
    curves = {k: replace(v, observation_date=on - timedelta(days=1)) for k, v in CURVES.items()}
    d = decide(scenario, curves=curves, on=on, now=later)
    assert (d[SHORT.id].status, d[SHORT.id].quoting_state) == ("none", "halted")
    assert d[CENTURY.id].status == "quoted"


def test_scenario_without_a_market_maker_quotes_nothing(tmp_path):
    path = tmp_path / "plain.sqlite3"
    create_scenario_db(path, settings(tmp_path), NOW)
    c = connect(path)
    d = decide(c)
    assert all(x.status == "none" for x in d.values())
    assert any("no market maker" in r for r in d[CENTURY.id].reasons)
