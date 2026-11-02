"""M8 step 5: the testnet venue and reconciliation, on a fake ledger with the real signer. Nothing touches a network."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from liveworld import ADDRESS, CFG, ISSUER, RLUSD, SEED, FakeNet, client_for, write_key

from debtlab.marketmaker.interfaces import Venue
from debtlab.xrpl_market import closetimes
from debtlab.xrpl_market.live import book, reconcile
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live.ledger import Header, TestnetLedger, exists_on_mainnet
from debtlab.xrpl_market.live.signer import Submitter, open_signer
from debtlab.xrpl_market.live.venue import Context, LiveOrder, LiveVenue, next_sequence, unresolved
from debtlab.xrpl_market.readonly import ReadOnlyClient, XrplReadError

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
MID = D("0.08")
REAL_TX = Path(__file__).parent / "fixtures" / "real" / "xrpl" / "tx_2026-11-01"
MAIN_RLUSD = "524C555344000000000000000000000000000000"
MAIN_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"


class Rig:
    def __init__(self, tmp_path, **net):
        self.net = FakeNet(**net)
        self.conn = ldb.open_db(tmp_path / "live.sqlite3")
        self.t = T0
        ldb.seed_limits(self.conn, CFG, T0)
        self.ledger = TestnetLedger(CFG, client_for(self.net))
        self.signer = open_signer(write_key(tmp_path), CFG, on_mainnet=lambda a: False, acl=lambda p: [], me=lambda: "me")
        self.venue = LiveVenue(self.signer, Submitter(CFG["endpoints"]["websocket"][0], connect=self.net), CFG)
        self.n = 0
        self.problems = []
        st = self.state()
        with ldb.tx(self.conn):
            book.open_account(self.conn, st, MID, D("1.12"), self.t)
        self.ctx()

    def state(self):
        return self.ledger.account(ADDRESS, self.ledger.latest().index)

    def ctx(self):
        h, st = self.ledger.latest(), self.state()
        self.venue.context = Context(h, next_sequence(self.conn, st.sequence), 10, MID)

    def place(self, side="buy", price="0.0799", size="10", replaces=None):
        self.n += 1
        o = LiveOrder(f"o{self.n}", "XRP/RLUSD", side, D(price), D(size), issuer=ISSUER, replaces=replaces)
        self.venue.submit(self.conn, o, self.t)
        return o.id

    def sync(self):
        self.t += timedelta(seconds=4)
        h = self.ledger.latest()
        st = self.ledger.account(ADDRESS, h.index)
        txs = self.ledger.transactions(ADDRESS, book.cursor(self.conn), h.index)
        with ldb.tx(self.conn):
            effects, self.problems = book.sync(self.conn, txs, st, h, MID, RLUSD, ISSUER, self.t)
        self.ctx()
        return effects

    def step(self, n=1):
        self.net.close_ledger(n)
        return self.sync()

    def order(self, oid):
        return dict(self.conn.execute("SELECT * FROM live_orders WHERE id = ?", (oid,)).fetchone())

    def txs(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM live_txs ORDER BY sequence")]

    def events(self, kind):
        return [json.loads(r["detail"]) for r in self.conn.execute("SELECT detail FROM live_events WHERE kind = ? ORDER BY id", (kind,))]


@pytest.fixture
def r(tmp_path):
    return Rig(tmp_path)


def test_it_is_a_venue_and_not_simulated(r):
    assert isinstance(r.venue, Venue) and r.venue.simulated is False and r.venue.label == "TESTNET: no real funds"


def test_the_books_open_from_the_ledger(r):
    assert book.balances(r.conn) == (D(300), D(20)) and book.cursor(r.conn) == 1000


def test_an_account_over_the_total_limit_opens_with_only_the_budget(tmp_path):
    r = Rig(tmp_path, xrp="1000", rlusd="100")
    xrp, rlusd = book.balances(r.conn)
    assert abs((xrp * MID + rlusd) / D("1.12") - 50) < D("0.00001") and xrp / rlusd == pytest.approx(10, abs=1e-6)
    assert book.set_aside(r.conn) == (D(1000) - xrp, D(100) - rlusd)
    r.step()
    assert r.problems == []


def test_money_in_beyond_the_budget_is_set_aside(tmp_path):
    r = Rig(tmp_path, xrp="1000", rlusd="100")
    budget, aside = book.balances(r.conn), book.set_aside(r.conn)
    r.net.payment(xrp="50")
    r.step()
    assert book.balances(r.conn) == budget and book.set_aside(r.conn) == (aside[0] + 50, aside[1]) and r.problems == []
    assert book.capital_eur(r.conn) <= 50


def test_money_in_fills_the_budget_and_only_then_is_set_aside(tmp_path):
    r = Rig(tmp_path)                       # EUR 39.29 in; EUR 10.71 of room is 12 RLUSD
    r.net.payment(rlusd="20")
    r.step()
    assert book.balances(r.conn)[1] == D(32) and book.set_aside(r.conn) == (0, D(8)) and r.problems == []


def test_money_out_comes_from_what_is_set_aside_first(tmp_path):
    r = Rig(tmp_path, xrp="1000", rlusd="100")
    budget, aside = book.balances(r.conn), book.set_aside(r.conn)
    r.net.payment(xrp="-10")
    r.step()
    assert book.balances(r.conn) == budget and book.set_aside(r.conn)[0] == aside[0] - 10 and r.problems == []


def test_outside_money_is_only_booked_once_even_if_read_twice(tmp_path):
    r = Rig(tmp_path, xrp="1000", rlusd="100")
    r.net.payment(xrp="50")
    r.net.close_ledger()
    h = r.ledger.latest()
    st = r.ledger.account(ADDRESS, h.index)
    txs = r.ledger.transactions(ADDRESS, 1000, h.index)
    with ldb.tx(r.conn):
        book.sync(r.conn, txs, st, h, MID, RLUSD, ISSUER, r.t)
        _, problems = book.sync(r.conn, txs, st, h, MID, RLUSD, ISSUER, r.t)
    assert problems == []


def test_an_account_with_no_trust_line_is_refused(tmp_path):
    net = FakeNet()
    net.has_line = False
    conn = ldb.open_db(tmp_path / "l.sqlite3")
    ldb.seed_limits(conn, CFG, T0)
    st = TestnetLedger(CFG, client_for(net)).account(ADDRESS, 1000)
    with pytest.raises(ldb.ControlError, match="no RLUSD trust line"):
        book.open_account(conn, st, MID, D("1.12"), T0)


@pytest.mark.parametrize("name,db", [("xrpl_snapshots", "the monitor's"), ("paper_orders", "the paper bot's"), ("events", "a bond")])
def test_it_refuses_other_databases(tmp_path, name, db):
    c = ldb.connect(tmp_path / "x.sqlite3")
    c.execute(f"CREATE TABLE {name} (id INTEGER)")
    c.close()
    with pytest.raises(ldb.WrongDatabase, match=db):
        ldb.open_db(tmp_path / "x.sqlite3")


def test_the_transaction_is_committed_before_it_is_sent(tmp_path):
    r = Rig(tmp_path)
    seen = []
    real = r.net.send

    def spying(raw):
        if json.loads(raw)["command"] == "submit":
            other = ldb.connect(tmp_path / "live.sqlite3")     # a second connection sees only what's committed
            seen.append(other.execute("SELECT COUNT(*) FROM live_txs").fetchone()[0])
            seen.append(other.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0])
            other.close()
        real(raw)
    r.net.send = spying
    r.place()
    assert seen == [1, 1]


def test_a_placed_offer_goes_pending_then_open_on_a_validated_ledger(r):
    oid = r.place()
    assert r.order(oid)["status"] == "pending" and unresolved(r.conn) == 1
    r.step()
    o = r.order(oid)
    assert o["status"] == "open" and o["applied_ledger"] == 1001 and r.problems == [] and unresolved(r.conn) == 0
    assert r.txs()[0]["status"] == "applied" and r.txs()[0]["result"] == "tesSUCCESS"


def test_every_offer_expires_on_the_rule_with_a_last_ledger(r):
    oid = r.place()
    (tx,) = r.net.blobs()
    assert tx["Expiration"] == closetimes.expiration_for(r.net.close_time, CFG["offer_expiry"]) == r.order(oid)["expiration"]
    assert tx["LastLedgerSequence"] == 1003 and tx["Flags"] & 0x00010000


def test_the_fee_is_journalled_and_the_balances_match_the_ledger(r):
    r.place()
    r.step()
    assert book.balances(r.conn) == (r.net.xrp / D(1_000_000), r.net.rlusd) and r.problems == []
    assert book.pnl(r.conn, MID).fees_paid_xrp == D("0.00001")


def test_replacing_is_one_transaction_that_removes_the_old_offer(r):
    a = r.place()
    r.step()
    b = r.place(price="0.0798", replaces=r.order(a)["sequence"])
    assert len(r.net.blobs()) == 2 and r.net.blobs()[1]["TransactionType"] == "OfferCreate"
    r.step()
    assert r.order(a)["status"] == "cancelled" and r.order(b)["status"] == "open"
    assert list(r.net.offers) == [r.order(b)["sequence"]] and r.problems == []


def test_a_cancel_removes_the_offer(r):
    a = r.place()
    r.step()
    r.venue.cancel(r.conn, [a], "test", r.t)
    r.step()
    assert r.order(a)["status"] == "cancelled" and r.net.offers == {} and r.problems == []


def test_an_offer_expires_on_ledger_time(r):
    a = r.place()
    r.step()
    exp = r.order(a)["expiration"]
    while r.net.close_time < exp:
        assert r.order(a)["status"] == "open"
        r.step()
    assert r.order(a)["status"] == "expired" and r.order(a)["reason"] == "reached its Expiration"


def test_an_expired_offer_still_on_the_ledger_isnt_a_disagreement(r):
    a = r.place()
    r.step()
    exp = r.order(a)["expiration"]
    while r.net.close_time < exp:
        r.step()
    assert list(r.net.offers) and r.problems == []     # still there, but it can't execute


def test_a_transaction_landing_past_its_expiration_is_tec_expired(r):
    a = r.place()
    r.net.parent_close = r.order(a)["expiration"]
    r.net.close_time = r.net.parent_close
    r.net.index += 1
    for tx in r.net.queue:
        r.net._apply(tx)
    r.net.queue = []
    r.sync()
    assert r.order(a)["status"] == "failed" and r.order(a)["reason"] == "tecEXPIRED"
    assert r.txs()[0]["status"] == "failed" and book.pnl(r.conn, MID).fees_paid_xrp == D("0.00001") and r.problems == []


def test_a_maker_fill_is_booked_at_its_price_and_matches_the_ledger(r):
    a = r.place(price="0.0799", size="10")
    r.step()
    r.net.hit(r.order(a)["sequence"], "4")
    effects = r.step()
    assert effects[0].maker_fills[0].xrp == D(4) and not effects[0].took
    o = r.order(a)
    assert o["status"] == "open" and D(o["filled_xrp"]) == 4 and D(o["took"]) == 0 and r.problems == []
    f = dict(r.conn.execute("SELECT * FROM live_fills").fetchone())
    assert f["role"] == "maker" and D(f["rlusd"]) == D("0.3196") and f["side"] == "buy"


def test_a_full_fill_closes_the_order(r):
    a = r.place(side="sell", price="0.0801", size="10")
    r.step()
    r.net.hit(r.order(a)["sequence"], "10")
    r.step()
    assert r.order(a)["status"] == "filled" and D(r.order(a)["filled_rlusd"]) == D("0.801") and r.problems == []


def test_a_fill_that_takes_liquidity_shows_as_taken(r):
    a = r.place(side="buy", price="0.0810", size="5")      # through the best offer at 0.0805
    effects = r.step()
    assert effects[0].took and effects[0].taker_xrp == D(5)
    o = r.order(a)
    assert o["status"] == "filled" and D(o["took"]) == D(5) and "took liquidity" in o["reason"]
    assert r.events("taker_fill")[0]["order"] == a and r.problems == []
    assert book.pnl(r.conn, MID).taker_fills == 1


def test_money_from_outside_is_external_not_profit(r):
    r.net.payment(xrp="5", rlusd="1")
    effects = r.step()
    assert effects[0].external and book.balances(r.conn) == (D(305), D(21)) and r.problems == []
    p = book.pnl(r.conn, MID)
    assert p.total == 0 and p.external == D(5) * MID + 1 and r.events("external_change")


def test_an_offer_the_bot_doesnt_know_is_a_disagreement(r):
    r.net.offers[55] = {"gets": "1000000", "pays": {"currency": RLUSD, "issuer": ISSUER, "value": "1"}, "expiration": None}
    r.step()
    assert r.problems == ["offer 55 is on the ledger but not in the books"]


def test_an_offer_on_another_pair_is_a_disagreement_too(r):
    r.net.offers[56] = {"gets": "1000000", "pays": {"currency": "USD", "issuer": "rOther", "value": "1"}, "expiration": None}
    r.step()
    assert "offer 56" in r.problems[0]


def test_a_balance_that_moved_without_a_transaction_is_a_disagreement(r):
    r.net.xrp += 1_000_000
    r.step()
    assert r.problems == ["XRP on the ledger 301, in the books 300"]
    r.net.xrp -= 1_000_000
    r.net.rlusd += 1
    r.step()
    assert r.problems == ["RLUSD on the ledger 21, in the books 20"]


def test_a_server_refusal_fails_the_order_at_once(r):
    r.net.force_prelim = "temBAD_OFFER"
    a = r.place()
    assert r.order(a)["status"] == "failed" and r.txs()[0]["status"] == "failed" and unresolved(r.conn) == 0


def test_no_connection_before_sending_fails_the_order(r):
    r.net.fail_connect = True
    a = r.place()
    assert r.order(a)["status"] == "failed" and r.txs()[0]["status"] == "failed"


def test_no_answer_to_a_submit_stays_pending_until_a_ledger_says(r):
    r.net.no_answer_to_submit = True
    a = r.place()
    assert r.order(a)["status"] == "pending" and r.txs()[0]["status"] == "pending"
    r.net.no_answer_to_submit = False
    r.step()
    assert r.order(a)["status"] == "open"        # it had reached the server after all


def test_a_transaction_that_never_lands_lapses_after_its_last_ledger(r):
    r.net.drop_all = True
    a = r.place()
    for _ in range(3):
        r.step()
        assert r.order(a)["status"] == "pending"
    r.step()
    assert r.order(a)["status"] == "failed" and r.txs()[0]["status"] == "not_applied"
    assert unresolved(r.conn) == 0 and r.events("tx_not_applied")


def test_the_next_sequence_runs_past_what_is_pending(r):
    r.place()
    r.ctx()
    assert r.venue.context.next_sequence == r.net.seq + 1
    r.place(side="sell", price="0.0801")
    r.step()
    assert r.net.seq == 102 and len(r.net.offers) == 2 and r.problems == []


def test_a_sync_reading_the_same_transaction_twice_books_it_once(r):
    r.place()
    r.net.close_ledger()
    h = r.ledger.latest()
    st = r.ledger.account(ADDRESS, h.index)
    txs = r.ledger.transactions(ADDRESS, 1000, h.index)
    with ldb.tx(r.conn):
        book.sync(r.conn, txs, st, h, MID, RLUSD, ISSUER, r.t)
        _, problems = book.sync(r.conn, txs, st, h, MID, RLUSD, ISSUER, r.t)
    assert problems == [] and book.pnl(r.conn, MID).fees_paid_xrp == D("0.00001")


def test_pnl_parts_add_up_exactly(r):
    a = r.place(price="0.0799", size="10")
    r.step()
    r.net.hit(r.order(a)["sequence"], "6")
    r.step()
    p = book.pnl(r.conn, D("0.0812"))
    assert p.spread + p.fees + p.inventory == p.total
    assert p.spread == D(6) * (MID - D("0.0799")) and p.maker_fills == 1


def test_daily_pnl_ignores_money_from_outside(r):
    book.record_day_open(r.conn, MID, r.t)
    r.t += timedelta(seconds=1)
    r.net.payment(xrp="10")
    r.step()
    assert book.daily_pnl_rlusd(r.conn, MID, r.t) == 0


# --- the parser on real transactions from the mainnet recording ------------------------------

def real_txs():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(REAL_TX.glob("tx_*.json"))]


def makers(msg):
    out = set()
    for w in msg["meta"]["AffectedNodes"]:
        (k, n), = w.items()
        f = n.get("FinalFields") or {}
        if n.get("LedgerEntryType") == "Offer" and "TakerGets" in (n.get("PreviousFields") or {}):
            out.add(f["Account"])
    return out


def test_the_parser_adds_up_on_every_saved_real_transaction():
    checked = 0
    for msg in real_txs():
        tx = msg.get("transaction") or msg.get("tx_json") or msg["tx"]
        for acct in makers(msg) | {tx["Account"]}:
            e = reconcile.effect(msg, acct, MAIN_RLUSD, MAIN_ISSUER)
            filled_x = sum((f.xrp if f.side == "buy" else -f.xrp for f in e.maker_fills), D(0))
            filled_r = sum((-f.rlusd if f.side == "buy" else f.rlusd for f in e.maker_fills), D(0))
            assert filled_x + e.taker_xrp + e.external_xrp - e.fee_xrp == e.xrp
            assert filled_r + e.taker_rlusd + e.external_rlusd + e.rounding_rlusd == e.rlusd
            checked += 1
    assert checked > 5


def test_a_real_makers_fill_matches_their_balance_change_when_nothing_else_moved():
    found = exact = 0
    for msg in real_txs():
        for acct in makers(msg):
            e = reconcile.effect(msg, acct, MAIN_RLUSD, MAIN_ISSUER)
            if e.ours or not e.maker_fills:
                continue
            found += 1
            assert not e.took
            exact += not e.external
    assert (found, exact) == (12, 12)


def test_the_two_real_fills_that_differ_are_rounding_not_fees():
    """Neither maker sent the transaction, so no fee is theirs; XRP is exact; only the 16th digit of RLUSD differs."""
    seen = {}
    for msg in real_txs():
        for acct in makers(msg):
            e = reconcile.effect(msg, acct, MAIN_RLUSD, MAIN_ISSUER)
            if e.rounding_rlusd:
                seen[acct] = (e.rounding_rlusd, e.ours, e.fee_xrp, e.external_xrp)
    assert seen == {"r3nvf3hBcb54sGDLfX3aMWBWjq3XEwadqo": (D("-1E-13"), False, 0, 0),
                    "rU8QEjAbNbXDnBR7kx9TLPXsAEng3axsz4": (D("5E-15"), False, 0, 0)}


def test_rounding_on_a_fill_keeps_the_books_exact_and_doesnt_stop_the_bot(r):
    a = r.place(price="0.0799", size="10")
    r.step()
    r.net.fill_rounding = D("-1E-13")
    r.net.hit(r.order(a)["sequence"], "4")
    (e,) = r.step()
    assert e.rounding_rlusd == D("-1E-13") and not e.external and r.problems == []
    p = book.pnl(r.conn, MID)
    assert p.spread + p.fees + p.inventory == p.total and p.external == 0


def test_a_difference_above_rounding_is_still_outside_money(r):
    a = r.place(price="0.0799", size="10")
    r.step()
    r.net.fill_rounding = D("1E-8")
    r.net.hit(r.order(a)["sequence"], "4")
    (e,) = r.step()
    assert e.external and e.external_rlusd == D("1E-8") and e.rounding_rlusd == 0 and r.problems == []


@pytest.mark.parametrize("low_is_us,balance,expected", [(True, "5", D(5)), (False, "-5", D(5))])
def test_a_trust_line_is_read_from_the_right_side(low_is_us, balance, expected):
    limits = ({"LowLimit": {"issuer": "rUs"}, "HighLimit": {"issuer": "rI"}} if low_is_us
              else {"LowLimit": {"issuer": "rI"}, "HighLimit": {"issuer": "rUs"}})
    msg = {"hash": "H", "ledger_index": 1, "tx": {"Account": "rOther", "TransactionType": "Payment"},
           "meta": {"TransactionResult": "tesSUCCESS", "AffectedNodes": [{"CreatedNode": {
               "LedgerEntryType": "RippleState", "NewFields": {"Balance": {"currency": "RL", "value": balance}, **limits}}}]}}
    assert reconcile.effect(msg, "rUs", "RL", "rI").rlusd == expected


# --- reads -----------------------------------------------------------------------------------

def test_the_ledger_reads_are_all_public_reads(r):
    from debtlab.xrpl_market.readonly import ALLOWED
    assert {"account_offers", "account_tx", "tx"} <= ALLOWED
    assert not ALLOWED & {"submit", "sign", "sign_for", "wallet_propose", "submit_multisigned"}


def test_a_real_client_refuses_to_submit_through_history():
    with pytest.raises(XrplReadError):
        ReadOnlyClient("https://testnet.xrpl-labs.com/").history("submit", {})


def test_the_ledger_header_is_read(r):
    h = r.ledger.latest()
    assert h == Header(1000, r.net.close_time, 10, 0)


def test_the_ledger_refuses_a_mainnet_config():
    from debtlab.xrpl_market import load_config
    from debtlab.xrpl_market.live.guards import MainnetBlocked
    with pytest.raises(MainnetBlocked):
        TestnetLedger(load_config())


class FakeMainnet:
    def __init__(self, url, answer):
        self.answer = answer

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def validated_index(self):
        return 1

    def at(self, *a):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_exists_on_mainnet_answers_from_the_ledger():
    assert exists_on_mainnet("rA", ["u"], lambda u: FakeMainnet(u, object())) is True
    assert exists_on_mainnet("rA", ["u"], lambda u: FakeMainnet(u, XrplReadError("account_info: actNotFound"))) is False


def test_not_being_able_to_check_mainnet_raises():
    with pytest.raises(XrplReadError, match="couldn't check"):
        exists_on_mainnet("rA", ["u", "v"], lambda u: FakeMainnet(u, XrplReadError("timeout")))


def test_no_seed_reaches_the_database(r):
    a = r.place()
    r.step()
    r.venue.cancel(r.conn, [a], "t", r.t)
    r.step()
    for row in r.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for rec in r.conn.execute(f"SELECT * FROM {row['name']}").fetchall():
            assert SEED not in " ".join(str(v) for v in tuple(rec))
