"""The day's P&L for the loss limit, from the ledger, never passed in (M4 step 6)."""

import inspect
from datetime import timedelta

import pytest

from debtlab import ledger
from debtlab.audit.db import recent_events
from debtlab.ledger import Line
from debtlab.marketmaker import controls, daily, execution, quoting
from debtlab.marketmaker.execution import Mark, check, place_order
from debtlab.money import D
from test_controls import CENTURY, INSTRUMENTS, MARKS, NOW, SHORT, ask, bid, open_db

BOND = {i.id: i for i in INSTRUMENTS}


@pytest.fixture
def running(tmp_path):
    c = open_db(tmp_path / "daily.sqlite3")
    controls.resume(c, "test", "go", NOW)
    yield c
    c.close()


def marked(**dirty):
    """MARKS with some dirty values changed, e.g. marked(century=95)."""
    out = dict(MARKS)
    for key, value in dirty.items():
        inst = {"century": CENTURY, "short": SHORT}[key]
        out[inst] = Mark(MARKS[inst].currency, D(value), MARKS[inst].accrued, MARKS[inst].dv01, MARKS[inst].data_age_hours)
    return out


def test_the_caller_cant_supply_the_days_pnl():
    for fn in (check, place_order, execution.enforce_daily_loss, quoting.decide_quotes):
        assert not any("pnl" in name for name in inspect.signature(fn).parameters), fn.__name__


def test_the_day_opens_once_then_a_new_day_opens_again(running):
    assert daily.daily_pnl(running, MARKS, NOW) == {"GBP": D(0), "USD": D(0)}
    first = daily.day_open(running, MARKS, NOW)
    assert first["position"][CENTURY] == D(1_000_000) and first["marks"][CENTURY] == ("GBP", D(94))
    assert daily.day_open(running, marked(century=90), NOW + timedelta(hours=5)) == first  # fixed for the day
    nxt = daily.day_open(running, marked(century=90), NOW + timedelta(days=1))
    assert nxt["day"] == "2026-10-31" and nxt["marks"][CENTURY] == ("GBP", D(90))
    assert len(recent_events(running, kind="mm_day_opened")) == 2


def test_pnl_by_hand_after_a_trade_and_a_mark_change(running):
    daily.day_open(running, MARKS, NOW)  # 1m century at 94
    s = ledger.trade(running, "t1", BOND[CENTURY], "market-maker", "market", D(100_000), D(90), NOW.date())
    # cash -s.cash; inventory 1.1m at 95 against 1m at 94
    expected = D(1_100_000) * 95 / 100 - D(1_000_000) * 94 / 100 - s.cash
    assert daily.daily_pnl(running, marked(century=95), NOW)["GBP"] == expected
    assert daily.daily_pnl(running, marked(century=95), NOW)["USD"] == D(0)  # the other currency is untouched


def test_money_put_in_isnt_pnl_but_fees_are(running):
    daily.day_open(running, MARKS, NOW)
    ledger.post(running, "top-up", "funding", NOW.date(), [Line("funding", "GBP", D(-100_000)),
                                                           Line("market-maker", "GBP", D(100_000))])
    assert daily.daily_pnl(running, MARKS, NOW)["GBP"] == D(0)
    ledger.post(running, "fee-1", "fee", NOW.date(), [Line("market-maker", "GBP", D(-10)), Line("funding", "GBP", D(10))])
    assert daily.daily_pnl(running, MARKS, NOW)["GBP"] == D(-10)


def test_a_bad_trade_in_the_ledger_trips_the_limit(running):
    place_order(running, ask(1, inst=SHORT, price="98"), MARKS, NOW)  # opens the day
    ledger.trade(running, "bad", BOND[CENTURY], "market-maker", "market", D(1_000_000), D(96), NOW.date())
    assert daily.daily_pnl(running, MARKS, NOW)["GBP"] < D(-20_000)  # paid well over the 94 dirty mark
    d = place_order(running, bid(1), MARKS, NOW)
    assert not d.allowed and d.paused and d.reasons[0].startswith("daily loss ")
    assert controls.state(running)["changed_by"] == "risk check"


def test_a_holding_that_cant_be_valued_makes_its_currency_unknown(running):
    no_short = {CENTURY: MARKS[CENTURY]}
    daily.day_open(running, MARKS, NOW)
    assert daily.daily_pnl(running, no_short, NOW) == {"GBP": D(0), "USD": None}
    running.execute("DELETE FROM instrument_currencies WHERE instrument_id = ?", (SHORT,))
    tomorrow = NOW + timedelta(days=1)
    assert daily.daily_pnl(running, no_short, tomorrow) == {"GBP": None, "USD": None}  # currency unknown: all in doubt
    assert "the day's GBP P&L is unknown: a holding can't be valued" in check(running, bid(1), no_short, tomorrow)
    assert not any("unknown" in r for r in check(running, ask(1), no_short, tomorrow))  # selling is still allowed


def test_a_bond_unvalued_at_the_open_is_marked_late(running):
    daily.day_open(running, {CENTURY: MARKS[CENTURY]}, NOW)  # USD curve missing at the open
    later = NOW + timedelta(hours=2)
    assert daily.daily_pnl(running, marked(short=97), later)["USD"] == D(0)  # opening mark taken now, late
    o = daily.day_open(running, MARKS, later)
    assert o["marks"][SHORT] == ("USD", D(97))
    assert daily.daily_pnl(running, MARKS, later)["USD"] == D(1_000_000) * (98 - 97) / 100
    [event] = recent_events(running, kind="mm_day_open_marked_late")
    assert event["payload"]["marks"][SHORT]["late"] == later.isoformat()
