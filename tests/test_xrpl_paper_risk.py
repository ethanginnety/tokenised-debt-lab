"""The paper risk check (M7 step 6): the brief's live limits in euro, every one tested."""

from decimal import Decimal as D

import pytest
from paperworld import ISSUER, World

from debtlab.xrpl_market.paper import account, controls
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.paper.market import read_market
from debtlab.xrpl_market.paper.risk import PaperOrder, PaperRiskCheck, iou_ok
from debtlab.xrpl_market.paper.venue import cancel_all, place, venues

LOOKALIKE = "rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK"


@pytest.fixture
def w(tmp_path):
    w = World(tmp_path)
    w.running_bot()
    cancel_all(w.paper, "clear for the test", w.at(), venues(None))
    assert account.open_orders(w.paper) == []
    return w


def order(side="buy", price="1.249", size="1", acct="conservative", issuer=ISSUER, oid=None, pair="XRP/RLUSD"):
    return PaperOrder(oid or f"t:{side}:{price}:{size}:{acct}", pair, side, D(price), D(size), account=acct, issuer=issuer)


def check(w, o, now=None, **kw):
    now = now or w.at()
    return PaperRiskCheck(w.cfg).check(w.paper, o, {"XRP/RLUSD": read_market(w.mon, now, w.cfg)}, now, **kw)


def put(w, o):
    m = read_market(w.mon, w.at(), w.cfg)
    return place(w.paper, o, m, w.at(), PaperRiskCheck(w.cfg), venues(m)[o.account])


def test_a_good_order_passes(w):
    assert check(w, order()) == [] and check(w, order("sell", "1.251")) == []


def test_eur_10_an_order(w):
    assert check(w, order(size="10")) == []                       # 12.49 RLUSD = EUR 9.992
    (r,) = check(w, order(size="10.01"))
    assert r == "order EUR 10.0000 over the maximum 10" or r.startswith("order EUR 10.00")


def test_two_open_orders_an_account(w):
    assert put(w, order(size="1", oid="a")) == [] and put(w, order("sell", "1.251", "1", oid="b")) == []
    assert any("2 orders already open on the conservative account; the maximum is 2" in r for r in check(w, order(oid="c")))
    assert check(w, order(oid="c", acct="optimistic")) == []       # per account
    assert check(w, order(oid="c"), replacing=frozenset({"a"})) == []


def test_the_total(w):
    pdb.set_limit(w.paper, "max_total_eur", D(15), "Ethan", "test", w.at())
    assert put(w, order(size="7", oid="a")) == []                 # EUR 6.99
    assert check(w, order("sell", "1.251", "7")) == []           # 6.9944 + 7.0056 = EUR 14.0000
    assert check(w, order("sell", "1.251", "8")) == ["open orders would total EUR 15.0008; the maximum is 15"]


def test_daily_loss_refuses_and_pauses(w):
    account.journal(w.paper, "optimistic", w.at(), None, "fee", D(-6), D(0), D("1.25"), "test-loss")   # EUR -6
    rs = check(w, order(acct="optimistic"))
    assert any("daily loss EUR 6.0000 on the optimistic account has reached the maximum 5" in r for r in rs)
    assert check(w, order()) == []                               # the other account's own check passes
    m = read_market(w.mon, w.at(), w.cfg)
    assert place(w.paper, order(acct="optimistic"), m, w.at(), PaperRiskCheck(w.cfg), venues(m)["optimistic"])
    st = pdb.state(w.paper)
    assert st["state"] == "paused" and st["changed_by"] == "risk check" and "optimistic account: daily loss" in st["reason"]


def test_funds_after_open_orders_with_the_reserve(w):
    pdb.set_limit(w.paper, "max_order_eur", D(100), "Ethan", "test", w.at())
    pdb.set_limit(w.paper, "max_total_eur", D(100), "Ethan", "test", w.at())
    xrp, rlusd = account.balances(w.paper, "conservative")
    reserve = D(1) + D("0.2") * 2                                 # base + trust line + this offer
    spare = xrp - reserve - 2 * D("0.00001")
    assert check(w, order("sell", "1.251", str(spare.quantize(D("0.000001"))))) == []
    assert any(r.startswith("not enough XRP on the conservative account") for r in
               check(w, order("sell", "1.251", str((spare + D("0.000002")).quantize(D("0.000001"))))))
    big = (rlusd / D("1.249")).quantize(D("0.000001"))
    assert check(w, order(size=str(big))) == []
    assert any(r.startswith("not enough RLUSD") for r in check(w, order(size=str(big + D("0.000002")))))
    assert put(w, order("sell", "1.251", "10", oid="s")) == []    # with an offer resting, less XRP is free
    assert any(r.startswith("not enough XRP") for r in check(w, order("sell", "1.252", str(spare - 9))))


def test_only_the_verified_issuer_and_pair(w):
    (r,) = check(w, order(issuer=LOOKALIKE))
    assert r == f"RLUSD issuer {LOOKALIKE} is a known look-alike; only {ISSUER}"
    assert check(w, order(issuer="")) == [f"RLUSD issuer (none) is not the verified RLUSD issuer; only {ISSUER}"]
    assert check(w, order(pair="XRP/USD")) == ["XRP/USD is not XRP/RLUSD"]


def test_passive_only(w):
    assert any("at or above the best offer 1.251: would take liquidity" in r for r in check(w, order(price="1.251")))
    assert any("at or below the best bid 1.249: would take liquidity" in r for r in check(w, order("sell", "1.249")))


def test_xrpl_precision(w):
    assert any("isn't whole drops" in r for r in check(w, order(size="1.0000001")))
    assert iou_ok(D("12.345678901234")) and not iou_ok(D("12.3456789012345678"))
    assert any("more than 15 significant digits" in r for r in check(w, order(price="1.234567891", size="1.234567")))


def test_stale_data_and_a_paused_bot(w):
    assert any(r.startswith("data 40s old") for r in check(w, order(), now=w.at(40)))
    controls.pause(w.paper, "Ethan", "test", w.at(), None)
    assert any(r.startswith("bot is paused") for r in check(w, order()))


def test_malformed_or_unknown_account(w):
    assert check(w, order(size="0")) == ["malformed order: buy 0 at 1.249"]
    assert check(w, order(acct="hopeful")) == ["no open paper account 'hopeful'"]
