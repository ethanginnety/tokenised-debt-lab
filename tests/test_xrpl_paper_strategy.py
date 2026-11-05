"""The RLUSD/XRP paper strategy (M7 step 5), and the M4 interfaces it plugs into."""

import json
import math
from datetime import date, timedelta
from decimal import Decimal as D

from paperworld import World

from debtlab.marketmaker import execution
from debtlab.marketmaker.interfaces import Mark, Order, RiskCheck, Strategy, Venue
from debtlab.xrpl_market import store
from debtlab.xrpl_market.paper import account, controls
from debtlab.xrpl_market.paper.market import read_market, realised_vol_bp
from debtlab.xrpl_market.paper.risk import PaperOrder, PaperRiskCheck
from debtlab.xrpl_market.paper.strategy import PaperStrategy, round_price
from debtlab.xrpl_market.paper.venue import PaperVenue


def test_the_interfaces_are_not_bond_specific(tmp_path):
    w = World(tmp_path)
    assert execution.Order is Order                             # M4's Order is the general one
    assert isinstance(PaperStrategy(w.cfg), Strategy)       # M4's own strategy: tests/test_interfaces.py
    assert isinstance(PaperRiskCheck(w.cfg), RiskCheck) and isinstance(execution.LedgerRiskCheck(), RiskCheck)
    v = PaperVenue("conservative")
    assert isinstance(v, Venue) and v.simulated and v.label == "PAPER"
    assert isinstance(PaperOrder("x", "XRP/RLUSD", "buy", D(1), D(1)), Order)
    assert isinstance(read_market(w.mon, w.at(), w.cfg), Mark)


def test_realised_volatility():
    mids = tuple(D(x) for x in ("1.25", "1.2501", "1.25", "1.2501", "1.25"))
    sigma, n = realised_vol_bp(mids)
    rets = [math.log(1.2501 / 1.25), math.log(1.25 / 1.2501)] * 2
    m = sum(rets) / 4
    assert n == 4 and abs(float(sigma) - math.sqrt(sum((r - m) ** 2 for r in rets) / 3) * 1e4) < 1e-5
    assert realised_vol_bp(mids[:2]) == (None, 1)


def test_prices_round_away_from_the_mid():
    assert round_price(D("1.2493411"), 6, up=False) == D("1.249341")
    assert round_price(D("1.2506581"), 6, up=True) == D("1.250659")


def test_a_quote_and_its_parts(tmp_path):
    w = World(tmp_path)
    for i in range(6):
        w.close(bid="1.249", ask="1.251" if i % 2 else "1.2512")      # mid alternates 1.25 / 1.2501
    controls.resume(w.paper, "Ethan", "test", w.at(0.1))
    m = read_market(w.mon, w.at(2), w.cfg)
    (q,) = PaperStrategy(w.cfg).decide(w.paper, m, w.at(2))
    c = {k: D(v) if isinstance(v, str) else v for k, v in q.components.items()}
    s = w.cfg["paper"]["strategy"]
    sigma, n = realised_vol_bp(m.mids)
    assert q.quoted and q.reasons == () and q.fair == m.mid == D("1.25")
    assert c["vol_bp"] == D(s["vol_mult"]) * sigma * D(s["vol_horizon_ledgers"]).sqrt() and c["vol_bp"] > 0
    assert c["age_bp"] == D(s["age_bp_per_second"]) * D(2) and c["age_s"] == 2.0
    assert q.bid_size == D(9) * D("1.25") / D("1.25") == D(9)              # EUR 9 at 1.25 USD/EUR and 1.25 RLUSD/XRP
    assert c["fee_bp"] == 2 * D("0.00001") / D(9) * 10_000
    assert c["half_spread_bp"] == c["base_bp"] + c["vol_bp"] + c["age_bp"] + c["fee_bp"]
    assert c["inventory_skew_bp"] == 0                                        # no account yet
    assert q.bid == round_price(D("1.25") * (1 - c["half_spread_bp"] / 10_000), 6, up=False)
    assert q.ask == round_price(D("1.25") * (1 + c["half_spread_bp"] / 10_000), 6, up=True)
    assert m.best_bid < q.ask and q.bid < m.best_ask                          # passive
    row = w.paper.execute("SELECT * FROM paper_decisions WHERE id = ?", (q.id,)).fetchone()
    assert row["quoted"] == 1 and D(row["bid_price"]) == q.bid and json.loads(row["components"])["vol_samples"] == n


def test_inventory_skews_both_prices(tmp_path):
    w = World(tmp_path)
    w.running_bot()
    base = PaperStrategy(w.cfg).quote(w.paper, read_market(w.mon, w.at(), w.cfg), w.at())
    assert D(base.components["driver_xrp_share"]) < D("0.51")          # opened half and half (less a fee)
    account.journal(w.paper, "conservative", w.at(), None, "fill", D(20), D(-25), D("1.25"), "test-long")
    long = PaperStrategy(w.cfg).quote(w.paper, read_market(w.mon, w.at(), w.cfg), w.at())
    skew = D(long.components["inventory_skew_bp"])
    assert 0 < skew <= D(10) and long.bid < base.bid and long.ask < base.ask
    assert D(long.components["ask_half_bp"]) >= D(w.cfg["paper"]["strategy"]["min_half_spread_bp"])


def _reasons(w, now):
    return PaperStrategy(w.cfg).quote(w.paper, read_market(w.mon, now, w.cfg), now).reasons


def test_no_quote_while_paused_or_killed(tmp_path):
    w = World(tmp_path)
    w.warm()
    assert any("bot is paused (never started)" in r for r in _reasons(w, w.at()))
    controls.resume(w.paper, "Ethan", "test", w.at())
    assert _reasons(w, w.at()) == ()
    controls.kill(w.paper, "Ethan", "test", w.at(), None)
    assert any("bot is killed" in r for r in _reasons(w, w.at()))


def test_no_quote_on_stale_data_or_an_outage(tmp_path):
    w = World(tmp_path)
    w.warm()
    controls.resume(w.paper, "Ethan", "test", w.at())
    assert any("data 31s old; the maximum is 30s" in r for r in _reasons(w, w.at(31)))
    store.open_outage(w.mon, "disconnected", w.at(1), "test drop")
    rs = _reasons(w, w.at(2))
    assert any("monitor outage open (disconnected: test drop)" in r for r in rs)


def test_no_quote_on_a_crossed_book_a_stale_rate_or_little_history(tmp_path):
    w = World(tmp_path, vol_min_samples=30)
    w.warm(3)
    controls.resume(w.paper, "Ethan", "test", w.at())
    assert any("only 2 mid changes in the last 15 min; need 30" in r for r in _reasons(w, w.at()))
    w.close(bid="1.252", ask="1.251")
    assert any(r.startswith("book invalid: prices crossed") for r in _reasons(w, w.at()))
    w2 = World(tmp_path / "old", fx_date=date(2026, 10, 27))
    w2.warm()
    controls.resume(w2.paper, "Ethan", "test", w2.at())
    assert any("EUR/USD rate of 2026-10-27 is stale" in r for r in _reasons(w2, w2.at()))


def test_no_quote_once_the_monitor_heartbeat_is_old(tmp_path):
    w = World(tmp_path)
    w.warm()
    controls.resume(w.paper, "Ethan", "test", w.at())
    later = w.t + timedelta(seconds=45)
    assert any("no monitor heartbeat for 45s" in r for r in _reasons(w, later))
