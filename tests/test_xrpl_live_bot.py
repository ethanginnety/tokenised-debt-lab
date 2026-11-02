"""M8 step 6: limits, controls and the bot's loop, on the fake ledger with the real signer. Nothing touches a network."""

import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
from liveworld import ADDRESS, CFG, ISSUER, T0, FakeNet, Monitor, client_for, write_key

from debtlab.marketmaker.interfaces import RiskCheck
from debtlab.xrpl_market import load_config
from debtlab.xrpl_market.live import book, controls
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live.bot import LiveBot, Want
from debtlab.xrpl_market.live.guards import MainnetBlocked
from debtlab.xrpl_market.live.ledger import TestnetLedger
from debtlab.xrpl_market.live.risk import LiveMark, LiveRiskCheck
from debtlab.xrpl_market.live.signer import Submitter, open_signer
from debtlab.xrpl_market.live.venue import LiveOrder, LiveVenue
from debtlab.xrpl_market.paper import market as mk

BID, ASK = D("0.0799"), D("0.0801")


class World:
    def __init__(self, tmp_path, **net):
        self.tmp = tmp_path
        self.net = FakeNet(**net)
        self.mon = Monitor(tmp_path)
        self.conn = ldb.open_db(tmp_path / "live.sqlite3")
        self.t = T0
        self.want = {"buy": Want(BID, D(100)), "sell": Want(ASK, D(100))}
        self.lines = []
        self.mon.snapshot(self.net.index, self.t)
        self.bot = self.make_bot()

    def make_bot(self):
        self.ledger = TestnetLedger(CFG, client_for(self.net))
        self.signer = open_signer(write_key(self.tmp), CFG, on_mainnet=lambda a: False, acl=lambda p: [], me=lambda: "me")
        self.sub = Submitter(CFG["endpoints"]["websocket"][0], connect=self.net)
        self.venue = LiveVenue(self.signer, self.sub, CFG)
        return LiveBot(self.conn, self.mon.path, CFG, self.ledger, self.venue, self.sub, lambda m, now: dict(self.want),
                       out=self.lines.append)

    def clock(self):
        return self.t

    def advance(self, dt=4.0, **snap):
        self.t += timedelta(seconds=dt)
        self.net.close_ledger()
        self.mon.snapshot(self.net.index, self.t, **snap)

    def step(self, n=1, **snap):
        r = None
        for _ in range(n):
            self.advance(**snap)
            r = self.bot.tick(self.t + timedelta(seconds=0.5))
        return r

    def running(self):
        self.bot.start(self.t)
        controls.resume(self.conn, "Ethan", "test", self.t)
        self.step(4)     # opens and places; applied; the first 10 s boundary replaces them; applied, with two ledgers left
        return self

    def covered(self):
        """Sides that can execute in the next ledger: resting and unexpired, or on their way."""
        ours = [{"pays": o["pays"]} for o in (self.net.offers[s] for s in self.live_offers())]
        ours += [{"pays": t["TakerPays"]} for t in self.net.queue if t["TransactionType"] == "OfferCreate"]
        return {"buy" if isinstance(o["pays"], str) else "sell" for o in ours}

    def sleep(self, _):
        self.advance(4.0)

    def state(self):
        return ldb.state(self.conn)["state"]

    def orders(self, *status):
        q = "SELECT * FROM live_orders" + (f" WHERE status IN ({','.join('?' * len(status))})" if status else "") + " ORDER BY sequence"
        return [dict(r) for r in self.conn.execute(q, status)]

    def events(self, kind):
        return [json.loads(r["detail"]) for r in self.conn.execute("SELECT detail FROM live_events WHERE kind = ? ORDER BY id", (kind,))]

    def live_offers(self):
        return [s for s, o in self.net.offers.items() if (o.get("expiration") or 10**12) > self.net.close_time]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_it_starts_paused_and_sends_nothing_until_resumed(w):
    w.bot.start(w.t)
    assert w.state() == "paused"
    w.step(3)
    assert w.net.blobs() == [] and book.account(w.conn) is None


def test_resuming_opens_the_account_and_quotes_both_sides(w):
    w.running()
    assert book.balances(w.conn)[1] == D(20) and len(w.net.offers) == 2
    txs = w.net.blobs()
    assert {t["TransactionType"] for t in txs} == {"OfferCreate"} and all(t["Flags"] & 0x00010000 for t in txs)
    assert len(w.orders("open")) == 2 and {o["price"] for o in w.orders("open")} == {"0.0799", "0.0801"}


def test_quotes_are_replaced_in_one_transaction_when_the_price_changes(w):
    w.running()
    old = {o["side"]: o["sequence"] for o in w.orders("open")}
    w.want["buy"] = Want(D("0.0798"), D(100))
    before = len(w.net.blobs())
    w.step()
    tx = w.net.blobs()[before]
    assert tx["OfferSequence"] == old["buy"] and tx["TransactionType"] == "OfferCreate" and len(w.net.blobs()) == before + 1
    w.net.close_ledger()
    assert old["buy"] not in w.net.offers and tx["Sequence"] in w.net.offers and len(w.net.offers) == 2


def test_offers_are_refreshed_before_they_can_lapse_while_quoting(w):
    w.running()
    for _ in range(40):
        w.step()
        assert w.state() == "running" and w.covered() == {"buy", "sell"}
        assert len(w.net.offers) <= 2           # expired offers don't pile up, holding reserve
    assert {t["TransactionType"] for t in w.net.blobs()} == {"OfferCreate"}
    assert not w.events("order_refused")


def test_an_offer_lives_within_the_budget_on_ledger_time(w):
    w.running()
    for t in w.net.blobs():
        assert t["Expiration"] - w.net.close_time <= 15


def test_no_offer_is_left_over_after_running_with_the_books_matching(w):
    w.running()
    w.step(12)
    w.bot.stop(w.clock, "test", sleep=w.sleep)
    assert w.net.offers == {} and w.events("run_stopped")[-1]["zero_offers"] is True


# --- every stop ------------------------------------------------------------------------------

def test_stale_data_cancels_everything_and_stops_quoting(w):
    w.running()
    w.t += timedelta(seconds=31)
    w.net.close_ledger()                       # the ledger moves; the monitor's data doesn't
    r = w.bot.tick(w.t)
    assert r["cancels"] == 2 and any("old" in p for p in r["problems"]) and w.state() == "running"
    n = len(w.net.blobs())
    w.t += timedelta(seconds=4)
    w.net.close_ledger()
    w.bot.tick(w.t)
    assert w.net.offers == {} and len(w.net.blobs()) == n


def test_quoting_comes_back_when_the_data_does(w):
    test_stale_data_cancels_everything_and_stops_quoting(w)
    w.step(2)
    assert len(w.net.offers) == 2


def test_the_monitor_going_down_cancels_everything(w):
    w.running()
    w.mon.conn.execute("INSERT INTO xrpl_outages(kind, started_at, detail) VALUES ('disconnected', ?, 'test')", (ldb.iso(w.t),))
    w.mon.conn.commit()
    r = w.bot.tick(w.t + timedelta(seconds=1))
    assert r["cancels"] == 2 and any("connection" in p for p in r["problems"])


def test_a_dropped_send_connection_stops_quoting_until_it_is_back(w):
    w.running()
    w.net.fail_connect = True
    w.sub.close()
    w.want["buy"] = Want(D("0.0798"), D(100))
    w.step()
    assert w.venue.down
    r = w.step()
    assert r["placed"] == 0 and any("connection" in p for p in r["problems"])
    w.net.fail_connect = False
    w.step(2)
    assert w.venue.down is None and len(w.live_offers()) == 2


def test_a_ledger_read_failure_tries_to_cancel_once_per_outage(w):
    w.running()
    w.net.reads_fail = True
    r = w.bot.tick(w.t + timedelta(seconds=1))
    assert r["cancels"] == 2 and "ledger read failed" in r["problems"][0]
    n = len(w.net.blobs())
    assert w.bot.tick(w.t + timedelta(seconds=2))["cancels"] == 0 and len(w.net.blobs()) == n
    assert len(w.events("connection_lost")) == 1


def test_a_budget_that_stops_holding_cancels_and_stops_quoting(w):
    w.running()
    w.net.resolution = 20
    r = w.step()
    assert r["cancels"] >= 1 and r["placed"] == 0 and any("budget" in p for p in r["problems"])


def test_a_ledger_closed_without_agreement_on_its_time_stops_quoting(w):
    w.running()
    w.net.close_flags = 1
    r = w.step()
    assert r["placed"] == 0 and any("budget" in p for p in r["problems"])


def test_a_fill_that_takes_liquidity_pauses_and_cancels(w):
    w.running()
    w.net.best_ask = D("0.0790")                # the book jumps below our bid, unseen by the monitor
    w.want["buy"] = Want(D("0.0798"), D(100))
    w.step(2)
    assert w.state() == "paused" and "took liquidity" in ldb.state(w.conn)["reason"]
    w.step(2)
    assert w.live_offers() == [] and book.pnl(w.conn, D("0.08")).taker_fills == 1


def test_money_from_outside_pauses(w):
    w.running()
    w.net.payment(xrp="1")
    w.step()
    assert w.state() == "paused" and "from outside" in ldb.state(w.conn)["reason"]


def test_an_unknown_offer_pauses_and_gets_cancelled(w):
    w.running()
    w.net.offers[55] = {"gets": "1000000", "pays": {"currency": CFG["rlusd"]["currency_hex"], "issuer": ISSUER, "value": "1"}, "expiration": None}
    w.step(2)
    assert w.state() == "paused" and "disagree" in ldb.state(w.conn)["reason"]
    w.step(2)
    assert 55 not in w.net.offers


def test_a_balance_mismatch_pauses_and_cancels(w):
    w.running()
    w.net.xrp += 5_000_000
    w.step()
    assert w.state() == "paused" and "XRP on the ledger" in ldb.state(w.conn)["reason"]
    w.step(2)
    assert w.live_offers() == []


def test_the_daily_loss_limit_pauses(w):
    w.running()
    w.step(1, bid="0.0595", ask="0.0605", amm_price="0.06")     # XRP falls 25% on about 300 XRP: about EUR 5.36
    assert w.state() == "paused" and "daily loss" in ldb.state(w.conn)["reason"]
    w.step(2)
    assert w.live_offers() == []


def test_a_paused_bot_cancels_on_its_next_tick_and_stays_quiet(w):
    w.running()
    controls.pause(w.conn, "Ethan", "test", w.t)
    assert w.step()["cancels"] == 2
    w.step()
    assert w.net.offers == {}
    n = len(w.net.blobs())
    w.step(3)
    assert len(w.net.blobs()) == n


# --- the kill switch -------------------------------------------------------------------------

def kill(w, **kw):
    return controls.kill(w.conn, w.ledger, w.venue, CFG, "Ethan", "test", 10, clock=w.clock, sleep=w.sleep, **kw)


def test_kill_leaves_zero_offers_on_the_ledger_with_the_bot_down(w):
    w.running()
    assert kill(w).zero and w.net.offers == {} and w.state() == "killed"


def test_kill_takes_expired_offers_off_the_ledger_too(w):
    w.running()
    w.t += timedelta(seconds=60)
    for _ in range(8):
        w.net.close_ledger()
    assert w.net.offers and not w.live_offers()
    assert kill(w).zero and w.net.offers == {}


def test_kill_works_from_a_fresh_process_and_an_unknown_offer(w):
    w.running()
    w.net.offers[55] = {"gets": "1000000", "pays": {"currency": CFG["rlusd"]["currency_hex"], "issuer": ISSUER, "value": "1"}, "expiration": None}
    conn2 = ldb.open_db(w.tmp / "live.sqlite3")
    w.make_bot()
    assert controls.kill(conn2, w.ledger, w.venue, CFG, "Ethan", "test", 10, clock=w.clock, sleep=w.sleep).zero
    assert w.net.offers == {}


def test_kill_also_covers_an_offer_still_on_its_way(w):
    w.running()
    w.want["buy"] = Want(D("0.0798"), D(100))
    w.advance()
    w.bot.tick(w.t)                            # sent, not validated
    assert kill(w).zero and w.net.offers == {}


def test_kill_retries_through_lost_cancels_until_zero(w):
    w.running()
    w.net.drop_all = True
    calls = []

    def sleep(_):
        calls.append(1)
        if len(calls) == 6:
            w.net.drop_all = False
        w.advance()
    assert controls.kill(w.conn, w.ledger, w.venue, CFG, "Ethan", "test", 10, clock=w.clock, sleep=sleep).zero
    assert w.net.offers == {}
    fees = [int(t["Fee"]) for t in w.net.blobs() if t["TransactionType"] == "OfferCancel"]
    assert fees[0] == 10 and max(fees) > 10


def test_kill_that_cant_finish_says_so_and_the_offers_expire_anyway(w):
    w.running()
    w.net.drop_all = True
    r = kill(w, timeout=10)
    assert not r.zero and r.remaining == 2 and w.events("cancel_incomplete") and w.state() == "killed"


def test_kill_state_is_committed_before_anything_is_sent(w):
    w.running()
    seen = []
    real = w.net.send

    def spying(raw):
        if json.loads(raw)["command"] == "submit":
            other = ldb.open_db(w.tmp / "live.sqlite3")
            seen.append(ldb.state(other)["state"])
            other.close()
        real(raw)
    w.net.send = spying
    kill(w)
    assert seen and set(seen) == {"killed"}


def test_kill_survives_a_restart_until_reset(w):
    w.running()
    kill(w)
    w.bot = w.make_bot()
    w.bot.start(w.t)
    assert w.state() == "killed"
    with pytest.raises(ldb.ControlError, match="kill switch is on"):
        controls.resume(w.conn, "Ethan", "x", w.t)
    w.step(3)
    assert w.net.offers == {}
    controls.reset(w.conn, "Ethan", "checked", w.t)
    assert w.state() == "paused"


def test_a_reset_needs_a_reason_and_a_kill(w):
    with pytest.raises(ldb.ControlError, match="not on"):
        controls.reset(w.conn, "Ethan", "x", w.t)
    controls.set_killed(w.conn, "Ethan", "x", w.t)
    with pytest.raises(ldb.ControlError, match="reason"):
        controls.reset(w.conn, "Ethan", "  ", w.t)


# --- restarts --------------------------------------------------------------------------------

def test_a_restart_pauses_and_cleans_up_what_was_left(w):
    w.running()
    left = set(w.net.offers)
    assert left
    w.bot = w.make_bot()
    w.bot.start(w.t + timedelta(seconds=1))
    assert w.state() == "paused" and "restarted" in ldb.state(w.conn)["reason"]
    w.step(2)
    assert not (left & set(w.net.offers)) and w.net.offers == {}


def test_a_restart_catches_up_on_fills_it_missed(w):
    w.running()
    o = w.orders("open")[0]
    w.net.hit(o["sequence"], "3")
    w.net.close_ledger()
    w.bot = w.make_bot()
    w.bot.start(w.t + timedelta(seconds=1))
    (r,) = [dict(x) for x in w.conn.execute("SELECT * FROM live_fills")]
    assert D(r["xrp"]) == 3 and w.state() == "paused" and "disagree" not in ldb.state(w.conn)["reason"]


def test_a_database_for_another_account_is_refused(w):
    w.running()
    w.conn.execute("UPDATE live_account SET address = 'rSomeoneElse'")
    w.conn.commit()
    with pytest.raises(ldb.ControlError, match="this database is for account"):
        w.make_bot().start(w.t)


# --- limits on what reaches the signer -------------------------------------------------------

def check(w, side="buy", price="0.0799", size="100", issuer=ISSUER, amm="0.08", resolution=10, replacing=frozenset(), oid="X"):
    now = w.t + timedelta(seconds=0.5)
    m = mk.read_market(w.bot.mon, now, CFG)
    h = w.ledger.latest()
    h = type(h)(h.index, h.close_time, resolution, h.close_flags)
    st = w.ledger.account(ADDRESS, h.index)
    return w.bot.risk.check(w.conn, LiveOrder(oid, "XRP/RLUSD", side, D(price), D(size), issuer=issuer),
                            {"XRP/RLUSD": LiveMark(m, h, st, None if amm is None else D(amm))}, now, replacing=replacing)


@pytest.fixture
def quiet(tmp_path):
    w = World(tmp_path)
    w.want = {"buy": None, "sell": None}
    w.running()
    return w


def test_the_check_is_a_risk_check():
    assert isinstance(LiveRiskCheck(CFG), RiskCheck)


def test_a_good_order_passes(quiet):
    assert check(quiet) == []


def test_each_limit_refuses(quiet):
    assert any("over the maximum" in r for r in check(quiet, size="150"))           # EUR 10.70
    assert any("refused issuer" in r or "not the verified" in r for r in check(quiet, issuer=load_config()["rlusd"]["issuer"]))
    assert any("not the verified" in r for r in check(quiet, issuer="rSomeone"))
    assert any("AMM's price is unknown" in r for r in check(quiet, amm=None))
    assert any("budget" in r for r in check(quiet, resolution=20))
    assert any("whole drops" in r for r in check(quiet, size="1.0000001"))


def test_the_margin_against_taking_covers_the_book_and_the_amm(quiet):
    assert any("best offer" in r for r in check(quiet, price="0.08049", size="100"))
    assert any("AMM price" in r for r in check(quiet, price="0.0799", amm="0.0798"))
    assert any("best bid" in r for r in check(quiet, side="sell", price="0.0795"))
    assert any("AMM price" in r for r in check(quiet, side="sell", price="0.0801", amm="0.0802"))


def test_expired_offers_on_the_ledger_count_towards_the_reserve(quiet):
    from debtlab.xrpl_market.live.risk import affordable
    h = quiet.ledger.latest()
    st = quiet.ledger.account(ADDRESS, h.index)
    m = mk.read_market(quiet.bot.mon, quiet.t + timedelta(seconds=0.5), CFG)
    room = affordable("sell", D("0.0801"), st, m, CFG, [])
    more = type(st)(st.address, st.ledger_index, st.xrp, st.rlusd, st.sequence, 10, st.has_trust_line, st.offers)
    assert affordable("sell", D("0.0801"), more, m, CFG, []) == room - D("0.2") * 8


def test_funds_come_from_the_ledger(quiet):
    quiet.net.rlusd = D(1)
    quiet.net.payment()           # nothing moved, but it's a new ledger
    quiet.step()
    assert any("not enough RLUSD" in r for r in check(quiet))


def test_the_total_limit_counts_capital_put_in(quiet):
    ldb.set_limit(quiet.conn, "max_total_eur", D(20), "Ethan", "test", quiet.t)
    assert any("capital in the account" in r for r in check(quiet))


def test_two_open_orders_is_the_maximum_and_a_bid_cant_cross_our_offer(tmp_path):
    w = World(tmp_path)
    w.running()
    assert any("orders already open" in r for r in check(w, side="sell", price="0.0802", oid="Y"))
    assert any("cross our own" in r for r in check(w, price="0.0802", oid="Z", amm="0.09"))


def test_a_refused_quote_never_reaches_the_signer_and_the_side_is_cancelled(w):
    w.running()
    n = len(w.net.blobs())
    w.want["buy"] = Want(D("0.0799"), D("150"))
    w.want["sell"] = Want(D("0.0801"), D(100))
    w.net.close_ledger()
    for _ in range(4):
        w.step()
    assert all(int(D(t["TakerPays"]) if isinstance(t["TakerPays"], str) else 0) < 150_000_000 for t in w.net.blobs()[n:])
    assert w.events("order_refused")[0]["side"] == "buy"
    assert len(w.events("order_refused")) == 1       # the same refusal isn't logged again and again


def test_size_is_cut_to_what_the_account_can_afford(tmp_path):
    w = World(tmp_path, rlusd="4")
    w.running()
    (b,) = [o for o in w.orders("open") if o["side"] == "buy"]
    assert D(b["size"]) <= D(4) / BID


# --- the network guards across the whole bot --------------------------------------------------

def test_every_signed_submit_goes_only_to_the_testnet_allowlist_across_a_full_scenario(w):
    w.running()
    w.step(10)
    w.want["buy"] = Want(D("0.0798"), D(100))
    w.step(3)
    while not w.orders("open"):
        w.step()
    w.net.hit(w.orders("open")[0]["sequence"], "1")
    w.step(2)
    kill(w)
    assert w.net.blobs() and set(w.net.urls) == {"wss://testnet.xrpl-labs.com/"}


def test_a_server_reporting_another_network_mid_run_stops_everything(w):
    calls, sent = [], []

    def sleep(_):
        calls.append(1)
        if len(calls) == 1:
            controls.resume(w.conn, "Ethan", "test", w.t)
        if len(calls) == 5:
            sent.append(len(w.net.blobs()))
            w.net.network_id = 0
        w.advance()
    with pytest.raises(MainnetBlocked):
        w.bot.run(clock=lambda: w.t, sleep=sleep)
    assert sent[0] > 0 and len(w.net.blobs()) == sent[0]
    assert w.state() == "killed" and "REFUSED" in w.lines[-1]


def test_a_server_reporting_another_network_at_start_sends_nothing(w):
    w.net.network_id = 0
    with pytest.raises(MainnetBlocked):
        w.bot.run(clock=lambda: w.t, sleep=lambda _: w.advance())
    assert w.state() == "killed" and w.net.blobs() == []


def test_the_bot_refuses_a_monitor_database_that_isnt_testnet(tmp_path):
    w = World(tmp_path)
    (tmp_path / "m").mkdir()
    main = Monitor(tmp_path / "m", network="mainnet")
    with pytest.raises(ldb.WrongDatabase, match="doesn't record testnet"):
        LiveBot(w.conn, main.path, CFG, w.ledger, w.venue, w.sub, lambda m, n: {})


def test_the_bot_refuses_a_mainnet_config(tmp_path):
    w = World(tmp_path)
    with pytest.raises(MainnetBlocked):
        LiveBot(w.conn, w.mon.path, load_config(), w.ledger, w.venue, w.sub, lambda m, n: {})


def test_a_missing_monitor_is_a_clear_error(tmp_path):
    w = World(tmp_path)
    with pytest.raises(FileNotFoundError, match="start the testnet monitor"):
        LiveBot(w.conn, tmp_path / "none.sqlite3", CFG, w.ledger, w.venue, w.sub, lambda m, n: {})


def test_an_account_with_no_trust_line_pauses_with_the_reason(tmp_path):
    w = World(tmp_path)
    w.net.has_line = False
    w.bot.start(w.t)
    controls.resume(w.conn, "Ethan", "test", w.t)
    w.step()
    assert w.state() == "paused" and "no RLUSD trust line" in ldb.state(w.conn)["reason"] and w.net.blobs() == []


# --- the EUR 50 budget -----------------------------------------------------------------------

def big_account(tmp_path, limit=None):
    w = World(tmp_path, xrp="1000", rlusd="100")
    if limit:
        ldb.seed_limits(w.conn, CFG, w.t)
        ldb.set_limit(w.conn, "max_total_eur", D(limit), "Ethan", "test", w.t)
    return w.running()


def test_a_big_account_isnt_refused_and_quotes_within_the_budget(tmp_path):
    w = big_account(tmp_path)
    assert w.state() == "running" and len(w.net.offers) == 2 and any("set aside" in x for x in w.lines)
    assert not w.events("order_refused")


def test_sizes_are_cut_to_the_budget_not_the_ledger(tmp_path):
    w = big_account(tmp_path, limit="5")          # EUR 5 of EUR 160.71: about 31 XRP and 3.1 RLUSD
    budget_x, budget_r = book.balances(w.conn)
    assert budget_x < 32 and budget_r < D("3.2")
    sizes = {o["side"]: D(o["size"]) for o in w.orders("open")}
    assert sizes["sell"] < budget_x and sizes["buy"] * BID <= budget_r


def test_pnl_and_the_daily_loss_ignore_what_is_set_aside(tmp_path):
    w = big_account(tmp_path)
    assert book.set_aside(w.conn)[0] > 600
    w.step(1, bid="0.0775", ask="0.0785", amm_price="0.078")      # 2.5% on 1000 XRP would be EUR 1.79
    p = book.pnl(w.conn, D("0.078"))
    xrp = book.balances(w.conn)[0]
    assert abs(p.inventory) < xrp * D("0.002") + D("0.01") and abs(p.inventory) < D(1000) * D("0.002") * D("0.5")


def test_the_books_keep_matching_a_big_account_through_fills(tmp_path):
    w = big_account(tmp_path)
    w.net.hit(w.orders("open")[0]["sequence"], "2")
    w.step(4)
    assert w.state() == "running" and "disagree" not in ldb.state(w.conn)["reason"]
    assert w.conn.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 1


# --- a crash ----------------------------------------------------------------------------------

def crash_after(w, ticks, error):
    calls, real = [], w.bot.tick

    def tick(now):
        calls.append(1)
        if len(calls) > ticks:
            raise error
        return real(now)
    w.bot.tick = tick

    def sleep(_):
        if len(calls) == 1:
            controls.resume(w.conn, "Ethan", "test", w.t)
        w.advance()
    return sleep


def test_a_crash_pauses_cancels_everything_and_still_shows_the_error(w):
    sleep = crash_after(w, 7, KeyError("unexpected field"))
    with pytest.raises(KeyError, match="unexpected field"):
        w.bot.run(clock=lambda: w.t, sleep=sleep)
    assert w.net.blobs() and w.net.offers == {} and w.state() == "paused"
    assert "crashed: KeyError" in ldb.state(w.conn)["reason"] and w.events("run_crashed")
    assert w.conn.execute("SELECT stop_reason FROM live_runs ORDER BY id DESC").fetchone()[0].startswith("crashed")


def test_a_crash_that_cant_cancel_still_pauses_and_shows_the_first_error(w):
    sleep = crash_after(w, 7, KeyError("first"))
    real = controls.cancel_until_zero

    def broken(*a, **k):
        raise ValueError("second")
    controls.cancel_until_zero = broken
    try:
        with pytest.raises(KeyError, match="first"):
            w.bot.run(clock=lambda: w.t, sleep=sleep)
    finally:
        controls.cancel_until_zero = real
    assert w.state() == "paused" and any("couldn't finish cancelling" in x for x in w.lines)


def test_a_crash_before_it_started_is_reported(w):
    w.bot.start = lambda now: (_ for _ in ()).throw(RuntimeError("no start"))
    with pytest.raises(RuntimeError, match="no start"):
        w.bot.run(clock=lambda: w.t, sleep=lambda _: None)
    assert any("crashed: RuntimeError" in x for x in w.lines)


def test_another_network_seen_while_stopping_turns_the_kill_switch_on(w):
    w.running()
    w.bot.tick = lambda now: (_ for _ in ()).throw(KeyboardInterrupt())
    w.net.network_id = 0
    w.bot.start = lambda now: None
    with pytest.raises(MainnetBlocked):
        w.bot.run(clock=lambda: w.t, sleep=lambda _: w.advance())
    assert w.state() == "killed"


# --- kill alongside a running bot ----------------------------------------------------------------

def second_process(w):
    """What `kill` from another window has: its own database connection and connection to the server."""
    conn = ldb.open_db(w.tmp / "live.sqlite3")
    return conn, LiveVenue(w.signer, Submitter(CFG["endpoints"]["websocket"][0], connect=w.net), CFG)


def test_kill_alongside_a_running_bot_reaches_zero_and_the_bot_places_nothing_more(w):
    w.running()
    conn, venue = second_process(w)
    before = len(w.net.blobs())

    def sleep(_):
        w.advance()
        w.bot.tick(w.t + timedelta(seconds=0.5))        # the bot carries on between kill's rounds
    r = controls.kill(conn, w.ledger, venue, CFG, "Ethan", "test", 10, clock=w.clock, sleep=sleep)
    assert r.zero and w.net.offers == {} and w.state() == "killed"
    assert {t["TransactionType"] for t in w.net.blobs()[before:]} == {"OfferCancel"}
    w.step(4)
    assert w.net.offers == {} and {t["TransactionType"] for t in w.net.blobs()[before:]} == {"OfferCancel"}


def test_kill_sharing_the_bots_ledger_doesnt_cancel_the_same_offer_twice(w):
    w.running()
    conn, venue = second_process(w)
    before = len(w.net.blobs())
    w.advance()
    w.bot.tick(w.t)
    controls.set_killed(conn, "Ethan", "test", w.t)
    w.bot.tick(w.t + timedelta(seconds=0.5))                # the bot sees it first and sends the cancels
    sent = [t for t in w.net.blobs()[before:] if t["TransactionType"] == "OfferCancel"]
    assert sent
    r = controls.kill(conn, w.ledger, venue, CFG, "Ethan", "test", 10, clock=w.clock, sleep=w.sleep)
    assert r.zero
    cancels = [t["OfferSequence"] for t in w.net.blobs()[before:] if t["TransactionType"] == "OfferCancel"]
    assert len(cancels) == len(set(cancels))


def test_kill_recovers_when_the_bot_takes_its_sequence_at_the_same_moment(w, monkeypatch):
    w.running()
    conn, venue = second_process(w)
    real, done = controls.cancel_offers, []

    def collide(c, v, read, reason, now, include_expired=False):
        if not done and v is venue:
            done.append(1)
            controls.read_ledger(w.conn, w.ledger, w.venue, CFG, None, 10, now)    # the bot read the same Sequence
            w.venue.cancel_offer(w.conn, read.account.offers[0].sequence, "the bot's own", now)
        return real(c, v, read, reason, now, include_expired)
    monkeypatch.setattr(controls, "cancel_offers", collide)
    r = controls.kill(conn, w.ledger, venue, CFG, "Ethan", "test", 10, clock=w.clock, sleep=w.sleep)
    assert done and r.zero and w.net.offers == {}
    assert conn.execute("SELECT COUNT(*) FROM live_txs WHERE preliminary = 'tefPAST_SEQ'").fetchone()[0] >= 1


def test_the_bot_carries_on_booking_after_a_kill_from_elsewhere(w):
    w.running()
    conn, venue = second_process(w)
    controls.kill(conn, w.ledger, venue, CFG, "Ethan", "test", 10, clock=w.clock, sleep=w.sleep)
    w.step(2)
    assert w.bot.last_read.problems == ()


def test_the_seed_goes_nowhere_across_a_full_scenario(w, capsys):
    from liveworld import SEED
    w.running()
    w.step(5)
    kill(w)
    for row in w.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for rec in w.conn.execute(f"SELECT * FROM {row['name']}").fetchall():
            assert SEED not in " ".join(str(v) for v in tuple(rec))
    out = capsys.readouterr()
    assert SEED not in out.out + out.err + " ".join(w.lines)
    assert SEED not in repr(w.signer) and SEED not in repr(w.venue.__dict__)
    assert all(SEED not in str(m) for m in w.net.sent)


def test_a_sequence_used_again_after_a_failed_send_gets_its_own_order(w):
    w.running()
    w.net.fail_connect = True
    w.sub.close()
    w.want["buy"] = Want(D("0.0798"), D(100))
    w.step()
    w.net.fail_connect = False
    w.step(3)
    seqs = [o["sequence"] for o in w.orders("failed")]
    assert seqs and w.state() == "running" and len(w.orders("open", "pending")) >= 1
    assert not w.events("order_refused")
