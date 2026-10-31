"""Market maker limits, pause and kill switch, enforced at execution and kept across restarts (M4 step 3)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from debtlab import ledger
from debtlab.api.app import create_app
from debtlab.audit.db import connect, migrate, recent_events
from debtlab.instruments import load_instruments
from debtlab.marketmaker import controls
from debtlab.marketmaker.book import load_book_config, seed_book
from debtlab.marketmaker.controls import ControlError
from debtlab.marketmaker.execution import Mark, Order, check, open_orders, place_order
from debtlab.money import D
from test_marketdata_audit_api import settings

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "marketmaker.json"
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.json")
CENTURY, SHORT = "CENTURY-GBP-2126", "SHORT-USD-2029"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
# Round TEST marks so the figures can be checked by hand (close to the real 29 Oct values).
MARKS = {
    CENTURY: Mark("GBP", dirty=D(94), accrued=D("3.5"), dv01=D("0.137"), data_age_hours=D(36)),
    SHORT: Mark("USD", dirty=D(98), accrued=D(1), dv01=D("0.021"), data_age_hours=D(36)),
}


def open_db(path):
    c = connect(path)
    migrate(c)
    ledger.seed(c, ledger.load_holders_config(ROOT / "config" / "holders.json"), INSTRUMENTS)
    seed_book(c, load_book_config(CONFIG), INSTRUMENTS)
    controls.seed_limits(c, CONFIG)
    return c


@pytest.fixture
def db(tmp_path):
    return tmp_path / "mm.sqlite3"


@pytest.fixture
def conn(db):
    c = open_db(db)
    yield c
    c.close()


@pytest.fixture
def running(conn):
    controls.resume(conn, "test", "start quoting", NOW)
    return conn


def bid(n, size=100_000, price="90", inst=CENTURY):
    return Order(f"bid-{n}", inst, "buy", D(price), D(size))


def ask(n, size=100_000, price="91", inst=CENTURY):
    return Order(f"ask-{n}", inst, "sell", D(price), D(size))


# ---------------------------------------------------------------- limits are stored


def test_limits_seeded_from_config_once(conn):
    assert len(controls.limits(conn)) == 14
    assert controls.limit(conn, "max_inventory", CENTURY) == D(2_000_000)
    assert controls.limit(conn, "max_exposure_dv01", "USD") == D(450)
    assert controls.limit(conn, "max_open_orders", "all") == D(4)
    assert controls.seed_limits(conn, CONFIG) == 0
    assert len(recent_events(conn, kind="limit_changed", limit=100)) == 14


def test_a_tightened_limit_survives_restart_and_a_looser_config(db, tmp_path):
    c = open_db(db)
    controls.set_limit(c, "max_order_size", CENTURY, D(50_000), "Ethan", "testing tighter size", NOW)
    c.close()
    looser = tmp_path / "loose.json"
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["limits"]["max_order_size"][CENTURY] = "9000000"
    looser.write_text(json.dumps(data), encoding="utf-8")

    c = open_db(db)  # a restart: migrate and seed again
    assert controls.seed_limits(c, looser) == 0
    assert controls.limit(c, "max_order_size", CENTURY) == D(50_000)
    controls.resume(c, "test", "go", NOW)
    assert "order size 100,000 over the maximum 50,000" in check(c, bid(1), MARKS, NOW)


def test_limit_changes_are_validated_and_versioned(conn):
    for args, message in [
        (("max_cheese", CENTURY, D(1)), "unknown limit"),
        (("max_inventory", "GBP", D(1)), "per instrument"),
        (("max_open_orders", CENTURY, D(1)), "per all"),
        (("max_daily_loss", "GBP", D(-1)), "negative"),
    ]:
        with pytest.raises(ControlError, match=message):
            controls.set_limit(conn, *args, "Ethan", "why")
    with pytest.raises(ControlError, match="who and why"):
        controls.set_limit(conn, "max_daily_loss", "GBP", D(1), "Ethan", " ")
    assert controls.set_limit(conn, "max_daily_loss", "GBP", D(20_000), "Ethan", "same") is False  # no change
    controls.set_limit(conn, "max_daily_loss", "GBP", D(10_000), "Ethan", "tighter")
    controls.set_limit(conn, "max_daily_loss", "GBP", D(15_000), "Ethan", "a bit looser")
    history = conn.execute("SELECT value, changed_by FROM mm_limit_changes WHERE name = 'max_daily_loss'"
                           " AND scope = 'GBP' ORDER BY id").fetchall()
    assert [(r["value"], r["changed_by"]) for r in history] == [
        ("20000", "config"), ("10000", "Ethan"), ("15000", "Ethan")]
    assert controls.limit(conn, "max_daily_loss", "GBP") == D(15_000)


def test_missing_limit_blocks_rather_than_allows(running):
    running.execute("DELETE FROM mm_limit_changes WHERE name = 'max_order_size'")
    assert "limit max_order_size for CENTURY-GBP-2126 is not set" in check(running, bid(1), MARKS, NOW)


# ---------------------------------------------------------------- the check


def test_never_started_means_paused(conn):
    assert controls.state(conn)["state"] == "paused"
    d = place_order(conn, bid(1), MARKS, NOW)
    assert not d.allowed and d.reasons == ("bot is paused (never started)",)
    assert open_orders(conn) == []
    assert recent_events(conn, kind="order_refused")[0]["payload"]["reasons"] == list(d.reasons)


def test_order_within_limits_is_placed_once(running):
    assert place_order(running, bid(1), MARKS, NOW).allowed
    again = place_order(running, bid(1), MARKS, NOW)
    assert again.allowed and again.already_placed
    assert [(o["id"], o["status"], o["price"], o["size"]) for o in open_orders(running)] == [
        ("bid-1", "open", "90", "100000")]
    with pytest.raises(ControlError, match="different terms"):
        place_order(running, bid(1, price="89"), MARKS, NOW)


def test_order_size(running):
    assert check(running, bid(1, size=250_000), MARKS, NOW) == []
    assert check(running, bid(1, size=250_001), MARKS, NOW) == ["order size 250,001 over the maximum 250,000"]


def test_open_orders(running):
    for n in range(4):
        assert place_order(running, ask(n, inst=SHORT, price="98"), MARKS, NOW).allowed
    assert check(running, bid(9), MARKS, NOW) == ["4 orders already open; the maximum is 4"]


def test_inventory_counts_open_bids(running):
    controls.set_limit(running, "max_inventory", CENTURY, D(1_200_000), "test", "tight")
    assert place_order(running, bid(1, size=150_000), MARKS, NOW).allowed  # 1.15m if it fills
    assert check(running, bid(2, size=100_000), MARKS, NOW) == [
        "inventory could reach 1,250,000 with open bids; the maximum is 1,200,000"]
    assert check(running, bid(2, size=50_000), MARKS, NOW) == []  # exactly 1.2m is allowed


def test_no_selling_more_than_held_including_open_offers(running):
    controls.set_limit(running, "max_open_orders", "all", D(10), "test", "room")
    for n in range(4):
        assert place_order(running, ask(n, size=250_000), MARKS, NOW).allowed  # all 1m offered
    assert check(running, ask(5, size=10_000), MARKS, NOW) == [
        "can't sell 10,000: 1,000,000 held, 1,000,000 already offered"]


def test_exposure_by_value_and_dv01(running):
    # 1m held + 250k bid = 1.25m face: 1.25m x 94% = £1,175,000 and 1.25m x 0.137 / 100 = £1,712.50 per bp.
    assert check(running, bid(1, size=250_000), MARKS, NOW) == []
    controls.set_limit(running, "max_exposure_value", "GBP", D(1_100_000), "test", "tight")
    controls.set_limit(running, "max_exposure_dv01", "GBP", D(1_700), "test", "tight")
    assert check(running, bid(1, size=250_000), MARKS, NOW) == [
        "GBP market value could reach 1,175,000.00; the maximum is 1,100,000",
        "GBP DV01 could reach 1,712.50 per bp; the maximum is 1,700",
    ]
    assert check(running, ask(1), MARKS, NOW) == []  # selling reduces exposure; never blocked by it
    assert check(running, bid(2, inst=SHORT, price="97"), MARKS, NOW) == []  # USD is separate


def test_data_age(running):
    old = {**MARKS, CENTURY: Mark("GBP", D(94), D("3.5"), D("0.137"), D(100))}
    assert check(running, ask(1), old, NOW) == ["data 100.0h old; the maximum is 96h"]
    assert check(running, ask(1, inst=SHORT, price="98"), old, NOW) == []  # its own curve is fresh


def test_cash_reserve_counts_open_bids(running):
    # £250,000 at 90 clean + 3.5 accrued costs £233,750; the bot has £1,500,000.
    controls.set_limit(running, "min_cash_reserve", "GBP", D(1_300_000), "test", "high")
    assert check(running, bid(1, size=250_000), MARKS, NOW) == [
        "GBP cash would fall to 1,266,250.00 if every bid fills; the reserve is 1,300,000"]
    controls.set_limit(running, "min_cash_reserve", "GBP", D(1_000_000), "test", "lower")
    assert place_order(running, bid(1, size=250_000), MARKS, NOW).allowed
    assert check(running, bid(2, size=50_000), MARKS, NOW) == []
    controls.set_limit(running, "min_cash_reserve", "GBP", D(1_230_000), "test", "between")
    # 1,500,000 - 233,750 - 50,000 x 93.5% = 1,219,500 < 1,230,000: the open bid counts.
    assert check(running, bid(2, size=50_000), MARKS, NOW) == [
        "GBP cash would fall to 1,219,500.00 if every bid fills; the reserve is 1,230,000"]


def test_every_breach_is_listed(running):
    controls.set_limit(running, "max_inventory", CENTURY, D(1_000_000), "test", "tight")
    old = {**MARKS, CENTURY: Mark("GBP", D(94), D("3.5"), D("0.137"), D(200))}
    reasons = check(running, bid(1, size=300_000), old, NOW)
    assert len(reasons) == 3 and reasons[0].startswith("order size") and reasons[1].startswith("data")


# ---------------------------------------------------------------- daily loss, pause, kill switch


def test_daily_loss_breach_refuses_pauses_and_cancels(running):
    place_order(running, ask(1, inst=SHORT, price="98"), MARKS, NOW)  # opens the day at these marks
    lower = {**MARKS, CENTURY: Mark("GBP", D(92), D("3.5"), D("0.137"), D(36))}  # the century falls 2 points
    d = place_order(running, bid(1), lower, NOW)  # 1m held x -2 / 100: a 20,000 loss, from the ledger
    assert not d.allowed and d.paused
    assert d.reasons == ("daily loss 20,000.00 GBP has reached the maximum 20,000",)
    st = controls.state(running)
    assert (st["state"], st["changed_by"]) == ("paused", "risk check")
    assert open_orders(running) == []  # the USD offer was pulled too
    assert running.execute("SELECT reason FROM mm_orders WHERE id = 'ask-1'").fetchone()[0].startswith("paused: daily loss")
    assert check(running, bid(2), MARKS, NOW) == [
        "bot is paused (daily loss 20,000.00 GBP has reached the maximum 20,000)"]


def test_pause_cancels_and_resume_starts_clean(running):
    place_order(running, bid(1), MARKS, NOW)
    assert controls.pause(running, "Ethan", "lunch", NOW) == 1
    assert open_orders(running) == [] and controls.state(running)["state"] == "paused"
    controls.resume(running, "Ethan", "back", NOW)
    assert open_orders(running) == []  # nothing comes back by itself
    assert place_order(running, bid(2), MARKS, NOW).allowed


def test_kill_switch(running):
    place_order(running, bid(1), MARKS, NOW)
    place_order(running, ask(1), MARKS, NOW)
    assert controls.kill(running, "Ethan", "something looks wrong", NOW) == 2
    assert open_orders(running) == [] and controls.state(running)["state"] == "killed"
    assert not place_order(running, bid(2), MARKS, NOW).allowed
    with pytest.raises(ControlError, match="reset it"):
        controls.resume(running, "Ethan", "oops")
    controls.pause(running, "Ethan", "pause doesn't undo a kill")
    assert controls.state(running)["state"] == "killed"
    with pytest.raises(ControlError, match="needs a reason"):
        controls.reset(running, "Ethan", "")
    controls.reset(running, "Ethan", "checked the data", NOW)
    assert controls.state(running)["state"] == "paused"  # not running: that's a separate decision
    assert not place_order(running, bid(2), MARKS, NOW).allowed
    controls.resume(running, "Ethan", "go", NOW)
    assert place_order(running, bid(2), MARKS, NOW).allowed
    kinds = [e["payload"]["to"] for e in reversed(recent_events(running, kind="mm_state_changed"))]
    assert kinds == ["running", "killed", "paused", "running"]


def test_kill_survives_restart(db):
    c = open_db(db)
    controls.resume(c, "test", "go", NOW)
    place_order(c, bid(1), MARKS, NOW)
    controls.kill(c, "Ethan", "stop", NOW)
    c.close()
    c = open_db(db)
    assert controls.state(c)["state"] == "killed"
    assert place_order(c, bid(2), MARKS, NOW).reasons == ("bot is killed (stop)",)
    assert open_orders(c) == []


def test_app_seeds_limits_in_main(tmp_path):
    with TestClient(create_app(settings(tmp_path))):
        pass
    c = connect(tmp_path / "t.sqlite3")
    assert len(controls.limits(c)) == 14 and controls.state(c)["state"] == "paused"
