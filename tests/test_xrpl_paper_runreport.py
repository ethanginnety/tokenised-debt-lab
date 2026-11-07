"""The end-of-run paper report (M7 step 7), on a fake monitor database. PAPER: nothing is sent anywhere."""

from decimal import Decimal as D

from paperworld import World

from debtlab.xrpl_market.paper import account, controls, runreport
from debtlab.xrpl_market.paper.db import parse
from debtlab.xrpl_market.paper.market import read_market


def price(w, side):
    (o,) = [o for o in w.orders() if o["side"] == side and o["account"] == "conservative"]
    return D(o["price"])


def quote_for(w, b, n, bid="1.249", ask="1.251"):
    """n ledgers of quoting, with an outside trade through our bid or offer every third ledger."""
    for i in range(n):
        trades = []
        if i % 3 == 1:
            trades = [("book", "sell_xrp", str(price(w, "buy") - D("0.001")), "2")]
        elif i % 3 == 2:
            trades = [("book", "buy_xrp", str(price(w, "sell") + D("0.001")), "2")]
        w.close(bid=bid, ask=ask, trades=trades)
        b.tick(w.at())


def two_sessions(tmp_path):
    """Session 1 at mid 1.25, ten minutes stopped while the mid falls to 1.20, session 2 at 1.20."""
    w = World(tmp_path)
    b = w.running_bot()
    quote_for(w, b, 30)
    b.stop(w.at(1), "stopped by user")
    for _ in range(150):                                       # 10 min of market, no bot
        w.close(bid="1.199", ask="1.201")
    b2 = w.bot()
    b2.start(w.at())
    controls.resume(w.paper, "Ethan", "session 2", w.at(0.6))
    quote_for(w, b2, 30, bid="1.199", ask="1.201")
    b2.stop(w.at(1), "stopped by user")
    return w


def test_the_windows_add_up_to_the_whole_run(tmp_path):
    w = two_sessions(tmp_path)
    q = [t for t, _ in runreport.quoted_times(w.paper)]
    whole = runreport.uptime_s(q, q[0], q[-1])
    cut_h = (whole - 40) / 3600                                 # the cutoff falls inside session 2
    r = runreport.build(w.paper, w.mon, w.cfg, cut_h)
    assert abs(r["uptime_h"]["main"] - cut_h) < 1e-9 and abs(r["uptime_h"]["after"] * 3600 - 40) < 1e-6
    end_mid = read_market(w.mon, parse(r["end"]), w.cfg).mid
    for a in r["accounts"]:
        main, after = a["windows"]["main"]["rlusd"], a["windows"]["after"]["rlusd"]
        p = account.pnl(w.paper, a["name"], end_mid)
        assert main["fills"] > 0 and after["fills"] > 0
        assert main["total"] + after["total"] == p.total                        # exactly
        assert main["spread"] + after["spread"] == p.spread and main["fees"] + after["fees"] == p.fees
        assert main["inventory"] + after["inventory"] == p.inventory
        for x in (main, after):
            assert x["spread"] + x["fees"] + x["inventory"] == x["total"]
        xrp, rlusd = account.balances(w.paper, a["name"])
        assert a["windows"]["after"]["end"]["xrp"] == xrp and a["windows"]["after"]["end"]["rlusd"] == rlusd


def test_the_move_while_stopped_is_inventory_while_not_quoting(tmp_path):
    w = two_sessions(tmp_path)
    r = runreport.build(w.paper, w.mon, w.cfg, 24)               # longer than the run: all of it is "main"
    (gap,) = [g for g in r["gaps"] if g["minutes"] > 5]
    a, b = parse(gap["from"]), parse(gap["to"])
    for acct in r["accounts"]:
        held = sum(D(x["xrp"]) for x in w.paper.execute("SELECT xrp FROM paper_journal WHERE account = ? AND at <= ?",
                                                        (acct["name"], gap["from"])))
        mids = dict(runreport.quoted_times(w.paper))
        moved = held * (mids[b] - mids[a])                       # what holding through the gap made or lost
        got = acct["windows"]["main"]["rlusd"]["inventory_not_quoting"]
        assert got < 0 and abs(got - moved) < D("0.0001")        # less only the stop's cancel fees in XRP
        assert acct["windows"]["after"]["rlusd"]["fills"] == 0


def test_the_report_says_paper_and_both_rules(tmp_path):
    w = two_sessions(tmp_path)
    r = runreport.build(w.paper, w.mon, w.cfg, 0.01)
    md = runreport.render(r)
    assert md.startswith("# PAPER") and "conservative" in md and "optimistic" in md and "upper bound" in md
    assert "Separate check" in md and "Bot versus doing nothing" in md
    assert '"label": "PAPER"' in runreport.to_json(r)
