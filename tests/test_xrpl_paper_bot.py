"""The paper bot end to end (M7 step 6), on a fake monitor database. PAPER: nothing is sent anywhere."""

import sqlite3
from datetime import timedelta
from decimal import Decimal as D

import pytest
from paperworld import World

from debtlab.audit.db import connect, migrate
from debtlab.xrpl_market import store
from debtlab.xrpl_market.paper import account, controls
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.paper.db import ControlError
from debtlab.xrpl_market.paper.market import read_market

FEE = D("0.00001")


def price(w, side, acct="conservative"):
    (o,) = [o for o in w.orders() if o["side"] == side and o["account"] == acct]
    return D(o["price"])


def fees(w, acct):
    return w.paper.execute("SELECT COUNT(*) FROM paper_journal WHERE account = ? AND kind = 'fee'", (acct,)).fetchone()[0]


def test_accounts_open_and_both_rest_the_same_quotes(tmp_path):
    w = World(tmp_path)
    w.running_bot()
    accts = account.accounts(w.paper)
    assert [a["name"] for a in accts] == ["conservative", "optimistic"] and accts[0]["start_eur"] == "50"
    for a in ("conservative", "optimistic"):
        xrp, rlusd = account.balances(w.paper, a)
        assert (xrp, rlusd) == (D(25) - 2 * FEE, D("31.25"))           # EUR 25 each side at 1.25 and 1.25, two fees
    orders = w.orders()
    assert len(orders) == 4 and {o["size"] for o in orders} == {"9"}
    assert price(w, "buy") == price(w, "buy", "optimistic") < D("1.25") < price(w, "sell") == price(w, "sell", "optimistic")
    assert w.events("accounts_opened") and len(w.events("order_placed")) == 4


def test_fills_come_only_from_later_ledgers_and_the_rules_differ(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    bid = price(w, "buy")
    placed = w.ledger
    store.record_trades(w.mon, [], w.at())
    from debtlab.xrpl_market.trades import Trade
    store.record_trades(w.mon, [Trade("S" * 64, "same", placed, "book", "sell_xrp", D("1.2"), D(9), D("10.8"), "rM",
                                      "OfferCreate")], w.at())       # in the ledger it was placed: never counts
    w.close(trades=[("book", "sell_xrp", str(bid), "3"), ("amm", "sell_xrp", "1.2400", "2")])
    assert b.tick(w.at())["fills"] == 0                             # one-ledger lag
    w.close()
    assert b.tick(w.at())["fills"] == 2
    got = {r["account"]: (D(r["xrp"]), D(r["mid"])) for r in w.paper.execute("SELECT * FROM paper_fills")}
    assert got == {"conservative": (D(2), D("1.25")), "optimistic": (D(5), D("1.25"))}
    xrp, rlusd = account.balances(w.paper, "optimistic")
    assert xrp == D(25) + 5 - 2 * FEE and rlusd == D("31.25") - 5 * bid
    (o,) = [o for o in w.orders() if o["account"] == "optimistic" and o["side"] == "buy"]
    assert D(o["filled"]) == 5 and o["status"] == "open"


def test_the_pnl_split_adds_up_exactly(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    bid, ask = price(w, "buy"), price(w, "sell")
    w.close(trades=[("book", "sell_xrp", "1.2400", "4")])
    b.tick(w.at())
    w.close(bid="1.2590", ask="1.2610", trades=[("book", "buy_xrp", "1.2700", "3")])   # mid up to 1.26
    b.tick(w.at())
    w.close(bid="1.2590", ask="1.2610")
    b.tick(w.at())
    w.close(bid="1.2440", ask="1.2460")                                                  # and down to 1.245
    b.tick(w.at())
    mid = D("1.245")
    for a in ("conservative", "optimistic"):
        p = account.pnl(w.paper, a, mid)
        assert p.spread + p.fees + p.inventory == p.total                               # exactly
        xrp, rlusd = account.balances(w.paper, a)
        assert p.total == account.value(xrp, rlusd, mid) - p.start_value
        assert p.fees < 0 and p.fees_paid_xrp == fees(w, a) * FEE
    fills = {(r["account"], r["side"]): r for r in w.paper.execute("SELECT * FROM paper_fills")}
    c = account.pnl(w.paper, "conservative", mid)
    # each fill against the mid in its own ledger: the offer was lifted after the mid jumped to 1.26,
    # so it shows as negative spread (adverse selection), as it should
    assert c.fills == 2 and c.spread == 4 * (D("1.25") - bid) + 3 * (ask - D("1.26")) and c.spread < 0
    assert D(fills[("conservative", "sell")]["mid"]) == D("1.26")
    assert fills[("conservative", "sell")]["order_id"].startswith("q1:sell")      # the first offer, cancelled since


def test_requote_only_when_the_price_moves_enough(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    before = {o["id"] for o in w.orders()}
    w.close(bid="1.24905", ask="1.25105")                         # mid up 0.4 bp
    b.tick(w.at())
    assert {o["id"] for o in w.orders()} == before
    w.close(bid="1.2510", ask="1.2530")                           # mid up 16 bp
    b.tick(w.at())
    after = w.orders()
    assert len(after) == 4 and not ({o["id"] for o in after} & before)
    assert all(r.startswith("requote:") for r in [o["reason"] for o in w.orders("cancelled")])
    assert fees(w, "conservative") == 2 + 2 + 2                    # placed, cancelled, placed again: both sides


def test_stale_data_cancels_everything_then_quoting_resumes(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    b.tick(w.at(31))
    assert w.orders() == [] and all(o["reason"].startswith("safety: data 31s old") for o in w.orders("cancelled"))
    w.close(dt=32)
    b.tick(w.at())
    assert len(w.orders()) == 4


def test_a_monitor_outage_cancels_everything(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    store.open_outage(w.mon, "disconnected", w.at(1), "test drop")
    b.tick(w.at(2))
    assert w.orders() == [] and "monitor outage open (disconnected: test drop)" in w.orders("cancelled")[0]["reason"]


def test_the_kill_switch_survives_a_restart(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    assert controls.kill(w.paper, "Ethan", "test", w.at(), read_market(w.mon, w.at(), w.cfg)) == 4
    b.stop(w.at(1), "test")
    b2 = w.bot()
    b2.start(w.at(2))
    w.close()
    b2.tick(w.at())
    assert pdb.state(w.paper)["state"] == "killed" and w.orders() == []
    with pytest.raises(ControlError, match="kill switch is on"):
        controls.resume(w.paper, "Ethan", "try", w.at())
    controls.reset(w.paper, "Ethan", "checked", w.at())
    assert pdb.state(w.paper)["state"] == "paused"
    controls.resume(w.paper, "Ethan", "go", w.at())
    w.close()
    b2.tick(w.at())
    assert len(w.orders()) == 4


def test_a_restart_cancels_what_was_left_and_never_uses_older_trades(tmp_path):
    w = World(tmp_path)
    w.running_bot()                                                # then "crashes": no stop
    bid = price(w, "buy")
    w.close(trades=[("book", "sell_xrp", str(bid - D("0.01")), "5")], dt=60)
    w.close()
    b2 = w.bot()
    b2.start(w.at())
    assert w.orders() == [] and all(o["reason"] == "restart: orders left open by the previous run"
                                    for o in w.orders("cancelled"))
    assert w.events("not_running")
    b2.tick(w.at(0.6))
    w.close()
    b2.tick(w.at())
    assert w.paper.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0


def test_a_daily_loss_pauses_the_bot(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    w.close(bid="0.999", ask="1.001")                              # mid 1.25 -> 1.00: 25 XRP lose EUR 5
    b.tick(w.at())
    st = pdb.state(w.paper)
    assert st["state"] == "paused" and st["changed_by"] == "risk check" and "daily loss EUR 5.0" in st["reason"]
    assert w.orders() == []


def test_markouts(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    bid = price(w, "buy")
    w.close(trades=[("book", "sell_xrp", "1.2400", "2")])
    b.tick(w.at())
    w.close()
    b.tick(w.at())                                                  # the fill is booked now
    for _ in range(80):                                             # 320 s later, mid up to 1.26
        w.close(bid="1.259", ask="1.261")
    mo = account.markouts(w.paper, w.mon, "conservative", w.at(), [60, 300])
    assert mo[60] == {"rlusd": 2 * (D("1.26") - bid), "fills": 1, "pending": 0}
    assert mo[300]["fills"] == 1
    early = account.markouts(w.paper, w.mon, "conservative", w.t - timedelta(seconds=300), [300])
    assert early[300]["pending"] == 1


def test_the_bot_reads_the_monitor_read_only(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        b.mon.execute("INSERT INTO xrpl_events(at, kind, detail) VALUES ('x', 'x', 'x')")


def test_the_paper_database_is_its_own(tmp_path):
    w = World(tmp_path)
    with pytest.raises(pdb.WrongDatabase, match="monitor's database"):
        pdb.open_db(w.mon_path)
    bond = connect(tmp_path / "bond.sqlite3")
    migrate(bond)
    bond.close()
    with pytest.raises(pdb.WrongDatabase, match="bond database"):
        pdb.open_db(tmp_path / "bond.sqlite3")


def test_run_loop_labels_paper_and_cancels_on_stop(tmp_path):
    w = World(tmp_path)
    w.warm()
    b = w.bot()
    clock = [w.at()]

    def tick_clock():
        return clock[0]

    def sleep(s):
        clock[0] += timedelta(seconds=s)
        if len(w.lines) == 1:                                      # resume after the first second
            controls.resume(w.paper, "Ethan", "test", clock[0])

    reason = b.run(until=w.at(3), clock=tick_clock, sleep=sleep)
    assert reason == "finished (--for)" and w.orders() == []
    assert all(line.startswith("PAPER") for line in w.lines)
    run = w.paper.execute("SELECT * FROM paper_runs").fetchone()
    assert run["stop_reason"] == "finished (--for)"
