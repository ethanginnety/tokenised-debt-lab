"""Mainnet session 1, 7 Nov 2026: the budget breach, and the fixes Ethan approved the same day.

What happened: each refresh replaces the old offer with an OfferCreate carrying OfferSequence, sized from
the last validated ledger as if the old offer were then removed untouched. When a taker filled the old
offer after that ledger (in the very ledger the replacement applied, before it, or in one the bot hadn't
read yet), the cancel found nothing, and the replacement still went up at full size: one quote's worth was
sold twice. 35 times in session 1; in the last, the budget went 1.881683 XRP below zero, into what was set
aside. On the ledger there was never more than one offer a side at once, so the two-order limit held.

The fake ledger applies other people's transactions before ours in a ledger, so a hit registered while
the replacement is queued fills the old offer first, exactly as on mainnet (ledger 108181372).

Fixes: (1) an offer being replaced counts in full against the budget and the EUR 50 total until its
replacement is settled; (2) a budget below zero on either side pauses the bot, saying why; (3) the fee is
the network's open-ledger cost read before each ledger's sends, with headroom and a cap, and offers get
more ledgers before they lapse.
"""

import json
import math
from datetime import timedelta
from decimal import Decimal as D

import pytest
from liveworld import CFG, FakeNet
from test_xrpl_live_bot import World, big_account, check

from debtlab.xrpl_market.live import book, compare, risk
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live import venue as lvenue
from debtlab.xrpl_market.live.bot import FEE_CAP_DROPS, FEE_HEADROOM, Want
from debtlab.xrpl_market.live.signer import Submitter

SIZE = D(100)


def sell_side_budget(tmp_path):
    """A big account, as on mainnet, with a budget of about 149 XRP: room for one and a half sells of 100."""
    w = big_account(tmp_path, limit="24")
    assert D(140) < book.balances(w.conn)[0] < D(150)
    return w


def open_sell(w):
    (s,) = [o for o in book.open_orders(w.conn, "sell") if o["status"] == "open"]
    return s


def race(w):
    """Move the sell's price so it is replaced; while the replacement is on its way, a taker fills the old
    offer in full, ahead of it in the same ledger; then a taker fills the replacement. Session 1's last case."""
    old = open_sell(w)
    w.want["sell"] = Want(D("0.0802"), SIZE)
    w.step()                                           # the replacement is signed and queued
    (new,) = [o for o in book.open_orders(w.conn, "sell") if o["status"] == "pending"]
    assert new["replaces"] == old["sequence"]
    w.net.hit(old["sequence"], str(SIZE))             # fills the old offer before the replacement applies
    w.step()
    w.net.hit(new["sequence"], str(SIZE))
    w.step(2)
    return old, new


def sold_xrp(w):
    return sum((D(r["xrp"]) for r in w.conn.execute("SELECT xrp FROM live_fills WHERE side = 'sell'")), D(0))


# --- (1) the breach, reproduced; then held -----------------------------------------------------------

def test_a_fill_on_the_offer_being_replaced_cant_take_the_budget_below_zero(tmp_path):
    """Fails on the code of session 1: the replacement went up at 100 XRP and both filled, 200 XRP from a
    budget of 149."""
    w = sell_side_budget(tmp_path)
    budget = book.balances(w.conn)[0]
    old, new = race(w)
    assert w.conn.execute("SELECT status FROM live_orders WHERE id = ?", (old["id"],)).fetchone()[0] == "filled"
    assert sold_xrp(w) <= budget
    assert book.balances(w.conn)[0] >= 0
    assert D(new["size"]) <= budget - SIZE           # sized with the old offer still counted in full


def test_control_without_counting_the_offer_being_replaced_the_breach_comes_back(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "replaced_for", lambda conn, order_id, replacing: [])
    monkeypatch.setattr("debtlab.xrpl_market.live.bot.replaced_for", lambda conn, order_id, replacing: [])
    w = sell_side_budget(tmp_path)
    budget = book.balances(w.conn)[0]
    race(w)
    assert sold_xrp(w) == 2 * SIZE > budget and book.balances(w.conn)[0] < 0


def test_with_room_for_two_sells_the_replacement_is_full_size(tmp_path):
    """The cost of the fix is only near the edge of the budget."""
    w = big_account(tmp_path, limit="40")             # about 249 XRP
    old = open_sell(w)
    w.want["sell"] = Want(D("0.0802"), SIZE)
    w.step()
    (new,) = [o for o in book.open_orders(w.conn, "sell") if o["status"] == "pending"]
    assert new["replaces"] == old["sequence"] and D(new["size"]) == SIZE


def test_without_room_for_both_the_old_offer_is_cancelled_and_quoted_afresh(tmp_path):
    """Room for one and a fraction: the replacement is cut to what fits beside the old offer; under the
    minimum, the side is cancelled and, once that's settled, quoted again at full size. Never both at once."""
    w = big_account(tmp_path, limit="18")             # about 112 XRP: 100 resting leaves under the minimum
    old = open_sell(w)
    w.want["sell"] = Want(D("0.0802"), SIZE)
    w.step(6)
    sells = [o for o in w.orders() if o["side"] == "sell" and o["sequence"] > old["sequence"]]
    assert sells and all(D(o["size"]) <= SIZE for o in sells)
    assert any(o["status"] in ("open", "pending") for o in sells)
    cancelled = w.conn.execute("SELECT status FROM live_orders WHERE id = ?", (old["id"],)).fetchone()[0]
    assert cancelled == "cancelled"
    assert book.balances(w.conn)[0] >= 0


def test_the_open_orders_limit_still_lets_a_replacement_through(tmp_path):
    """The two-order limit counts offers on the ledger at once. A replacement removes its old offer in the
    same transaction (or finds it gone), so it never makes three; the old one isn't counted twice there."""
    w = big_account(tmp_path, limit="40")
    sell = open_sell(w)
    reasons = check(w, side="sell", price="0.0802", size="10", replacing=frozenset({sell["id"]}), oid="R")
    assert not any("orders already open" in r for r in reasons)
    assert any("orders already open" in r for r in check(w, side="sell", price="0.0802", size="10", oid="R2"))


def test_the_eur_total_counts_the_offer_being_replaced(tmp_path):
    w = big_account(tmp_path, limit="40")
    sell = open_sell(w)
    # Two open orders of about EUR 7.15 each. With the cap at EUR 20, a third EUR 7.15 fits only if the old
    # sell is forgotten: 7.15 + 7.15 = 14.3 against 7.15 + 7.15 + 7.16 = 21.5.
    ldb.set_limit(w.conn, "max_total_eur", D(20), "Ethan", "test", w.t)
    reasons = check(w, side="sell", price="0.0802", size="100", replacing=frozenset({sell["id"]}), oid="R")
    assert any("open orders would total" in r for r in reasons)


def test_a_replacement_of_a_bid_counts_the_old_bids_rlusd(tmp_path):
    w = World(tmp_path, xrp="300", rlusd="12")       # 12 RLUSD: one bid of 100 at 0.0799 (7.99), not two
    w.running()
    (bid,) = [o for o in book.open_orders(w.conn, "buy") if o["status"] == "open"]
    reasons = check(w, side="buy", price="0.0798", size="100", replacing=frozenset({bid["id"]}), oid="R")
    assert any("not enough RLUSD" in r for r in reasons)


# --- (2) a budget below zero pauses ---------------------------------------------------------------

def test_a_budget_below_zero_pauses_and_says_why(tmp_path, monkeypatch):
    """The guard behind (1): if anything still takes a side below zero, the bot stops rather than sitting
    for 29 minutes refusing both sides, as at the end of session 1."""
    monkeypatch.setattr(risk, "replaced_for", lambda conn, order_id, replacing: [])
    monkeypatch.setattr("debtlab.xrpl_market.live.bot.replaced_for", lambda conn, order_id, replacing: [])
    w = sell_side_budget(tmp_path)
    race(w)
    assert w.state() == "paused"
    assert "below zero" in ldb.state(w.conn)["reason"]
    assert any("below zero" in x for x in w.lines)
    assert not w.live_offers()


def test_a_budget_at_zero_or_above_doesnt_pause(tmp_path):
    w = sell_side_budget(tmp_path)
    race(w)
    assert w.state() == "running"


# --- (3) the fee ----------------------------------------------------------------------------------

class Busy(FakeNet):
    """A server reporting the open-ledger cost as a multiple of the base fee, as rippled's server_info does."""

    def __init__(self, *a, load_factor=None, **k):
        super().__init__(*a, **k)
        self.load_factor = load_factor

    def send(self, raw):
        m = json.loads(raw)
        if m["command"] == "server_info" and self.load_factor is not None and not self.fail_connect:
            self.sent.append(m)
            self._out.append(json.dumps({"id": m["id"], "status": "success", "result": {"info": {
                "network_id": self.network_id, "load_factor": self.load_factor,
                "validated_ledger": {"base_fee_xrp": 0.00001}}}}))
            return
        return super().send(raw)


def busy_world(tmp_path, load_factor):
    w = World(tmp_path)
    w.net = Busy(load_factor=load_factor)
    w.bot = w.make_bot()
    return w


def test_the_submitter_reads_the_open_ledger_cost_from_server_info(tmp_path):
    net = Busy(load_factor=3.2)
    sub = Submitter(CFG["endpoints"]["websocket"][0], connect=net)
    assert sub.open_ledger_fee_drops() == 32
    assert [m["command"] for m in net.sent] == ["server_info"]


def test_no_open_ledger_cost_reported_is_none(tmp_path):
    sub = Submitter(CFG["endpoints"]["websocket"][0], connect=FakeNet())
    assert sub.open_ledger_fee_drops() is None


def test_offers_pay_the_open_ledger_cost_with_headroom_when_the_network_is_busy(tmp_path):
    w = busy_world(tmp_path, 3.2)
    w.running()
    fees = {int(t["Fee"]) for t in w.net.blobs()}
    assert fees == {math.ceil(32 * FEE_HEADROOM)}


def test_the_fee_is_capped(tmp_path):
    w = busy_world(tmp_path, 500)
    w.running()
    assert {int(t["Fee"]) for t in w.net.blobs()} == {FEE_CAP_DROPS}
    assert FEE_CAP_DROPS == 100                       # 0.0001 XRP, as Ethan approved


def test_a_quiet_network_pays_the_base_fee(tmp_path):
    w = busy_world(tmp_path, 1)
    w.running()
    assert {int(t["Fee"]) for t in w.net.blobs()} == {math.ceil(10 * FEE_HEADROOM)}


def test_without_a_reading_the_monitors_fee_is_used(tmp_path):
    w = World(tmp_path)
    w.running()
    assert {int(t["Fee"]) for t in w.net.blobs()} == {10}


def test_offers_get_five_ledgers_before_they_lapse(tmp_path):
    """Three ahead of a validated read that can itself be a ledger behind left too little room to queue."""
    assert lvenue.LAST_LEDGER_AHEAD == 5
    w = World(tmp_path).running()
    w.want["buy"] = Want(D("0.0798"), SIZE)
    n = len(w.net.blobs())
    w.step()                                           # signed against the ledger just read
    (tx,) = w.net.blobs()[n:]
    assert tx["LastLedgerSequence"] == w.net.index + 5


# --- (6) the comparison marks fills of a replacement sent after its old offer had filled ------------

def test_compare_marks_the_fills_of_a_replacement_whose_old_offer_filled_unseen(tmp_path):
    w = sell_side_budget(tmp_path)
    old, new = race(w)
    marked = compare.double_offer_fills(w.conn)
    on_new = {r["id"] for r in w.conn.execute("SELECT id FROM live_fills WHERE order_id = ?", (new["id"],))}
    on_old = {r["id"] for r in w.conn.execute("SELECT id FROM live_fills WHERE order_id = ?", (old["id"],))}
    assert on_new and marked == on_new and not (marked & on_old)


def test_an_ordinary_fill_isnt_marked(tmp_path):
    w = big_account(tmp_path, limit="40")
    w.net.hit(open_sell(w)["sequence"], "5")
    w.step(3)
    assert w.conn.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 1
    assert compare.double_offer_fills(w.conn) == set()


# --- session 2: budget period 2 (Ethan, 8 Nov 2026) ------------------------------------------------

def test_the_mainnet_script_defaults_to_budget_period_2_not_session_1s_databases():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import xrpl_mainnet
    assert xrpl_mainnet.BUDGET_PERIOD == 2
    assert xrpl_mainnet.DEFAULT_DB.name == "xrpl_mainnet_live_period2.sqlite3"
    assert xrpl_mainnet.DEFAULT_PAPER_DB.name == "xrpl_mainnet_paper_period2.sqlite3"
    assert "period" not in xrpl_mainnet.DEFAULT_SHADOW_DB.name
