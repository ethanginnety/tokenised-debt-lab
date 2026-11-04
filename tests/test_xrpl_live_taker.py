"""The crossing helper picks a live bot offer, prices through it and reports where the fill went (M8)."""

from __future__ import annotations

import importlib.util
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from xrpl.core import keypairs
from xrpl.core.binarycodec import decode

from debtlab.xrpl_market import load_testnet_config
from debtlab.xrpl_market.live.ledger import LedgerOffer
from debtlab.xrpl_market.readonly import XrplReadError

ROOT = Path(__file__).resolve().parent.parent
CFG = load_testnet_config()
BOT = "rpyoPHcW98szAtiw4AKwXNKGewHTwZdc6b"
OTHER = "rh88ZikPxxxxxxxxxxxxxxxxxxxxxxxxxx"
RLUSD = {"currency": CFG["rlusd"]["currency_hex"], "issuer": CFG["rlusd"]["issuer"]}
SEED = keypairs.generate_seed(entropy="22" * 16)
TAKER = "rhgwGnaFgbCGiCgxRsvpDnWpaBkmRzhi63"


def load():
    spec = importlib.util.spec_from_file_location("taker_helper", ROOT / "tests" / "live_testnet" / "taker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t = load()


def bid(seq=1, xrp="75.042211", rlusd="5.999997681305889", exp=100):
    return LedgerOffer(seq, "buy", D(xrp), D(rlusd), exp)


def ask(seq=2, xrp="8.4", rlusd="0.6729", exp=100):
    return LedgerOffer(seq, "sell", D(xrp), D(rlusd), exp)


def rl(value):
    return {**RLUSD, "value": value}


# --- which offer, at what price ---------------------------------------------------------------

def test_an_expired_offer_is_skipped_though_the_ledger_still_lists_it():
    assert t.find_target((bid(exp=100),), "sell", 100, D(5)) is None
    assert t.find_target((bid(exp=101),), "sell", 100, D(5)).sequence == 1


def test_the_side_is_the_opposite_of_the_takers_and_the_longest_lived_wins():
    offers = (bid(1, exp=110), bid(3, exp=120), ask(2, exp=130))
    assert t.find_target(offers, "sell", 100, D(5)).sequence == 3
    assert t.find_target(offers, "buy", 100, D(5)).sequence == 2


def test_an_offer_smaller_than_the_size_is_skipped_so_nothing_spills_elsewhere():
    assert t.find_target((ask(xrp="4", rlusd="0.32"),), "buy", 50, D(5)) is None


def test_the_limit_is_through_the_exact_rate_not_the_rounded_one():
    # The real offer behind the first killed attempt: `status` said 0.079955, the ledger's rate is lower.
    o = bid()
    assert o.rlusd / o.xrp < D("0.079955")
    limit = t.limit_through(o, "sell", 6)
    assert limit == D("0.079954") and limit < o.rlusd / o.xrp


def test_a_rate_exactly_on_a_tick_still_moves_one_tick():
    assert t.limit_through(bid(xrp="100", rlusd="8"), "sell", 6) == D("0.079999")
    assert t.limit_through(ask(xrp="100", rlusd="8"), "buy", 6) == D("0.080001")


def test_buying_crosses_upward():
    o = ask(xrp="8.399628", rlusd="0.672927797592")
    assert t.limit_through(o, "buy", 6) > o.rlusd / o.xrp


# --- where the fill went ----------------------------------------------------------------------

def offer_node(owner, seq, prev_gets, gets, prev_pays, pays, kind="ModifiedNode"):
    return {kind: {"LedgerEntryType": "Offer", "FinalFields": {"Account": owner, "Sequence": seq, "TakerGets": gets, "TakerPays": pays},
                   "PreviousFields": {"TakerGets": prev_gets, "TakerPays": prev_pays}}}


def expired_node(owner, seq):
    # As in the real killed attempts: deleted, with no change in its amounts.
    return {"DeletedNode": {"LedgerEntryType": "Offer", "FinalFields": {"Account": owner, "Sequence": seq, "TakerGets": rl("6"), "TakerPays": "75000000"}}}


def amm_node(prev, final):
    return {"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": "rAMM", "AMMID": "AB", "Balance": final},
                             "PreviousFields": {"Balance": prev}}}


def test_a_fill_on_the_bots_offer_is_reported_with_its_amounts():
    meta = {"TransactionResult": "tesSUCCESS", "AffectedNodes": [offer_node(BOT, 7, rl("6"), rl("5.6"), "75000000", "70000000")]}
    o = t.read_outcome(meta, BOT)
    assert o.hit_bot and o.ours == ((7, D(5), D("0.4")),) and not o.others and not o.amm_xrp
    assert "HIT THE BOT'S OFFER, sequence 7: 5 XRP for 0.4 RLUSD" in t.describe(o, BOT)


def test_a_fully_consumed_bot_offer_counts_as_a_fill():
    meta = {"TransactionResult": "tesSUCCESS", "AffectedNodes": [offer_node(BOT, 7, "5000000", "0", rl("0.4"), rl("0"), "DeletedNode")]}
    assert t.read_outcome(meta, BOT).ours == ((7, D(5), D("0.4")),)


def test_a_fill_on_the_amm_or_another_offer_isnt_a_hit_on_the_bot():
    meta = {"TransactionResult": "tesSUCCESS", "AffectedNodes": [amm_node("1000000000", "1005000000")]}
    o = t.read_outcome(meta, BOT)
    assert not o.hit_bot and o.amm_xrp == D(5)
    assert "hit the AMM: 5 XRP" in t.describe(o, BOT)
    meta = {"TransactionResult": "tesSUCCESS", "AffectedNodes": [offer_node(OTHER, 9, rl("6"), rl("5.6"), "75000000", "70000000")]}
    o = t.read_outcome(meta, BOT)
    assert not o.hit_bot and o.others == ((OTHER, 9, D(5), D("0.4")),)


def test_an_expired_offer_removed_by_a_killed_attempt_isnt_a_fill():
    meta = {"TransactionResult": "tecKILLED", "AffectedNodes": [expired_node(BOT, 22047326)]}
    o = t.read_outcome(meta, BOT)
    assert not o.hit_bot and o.removed == ((BOT, 22047326),)
    text = t.describe(o, BOT)
    assert "nothing traded" in text and any("already expired" in line for line in text)


# --- the loop ---------------------------------------------------------------------------------

class World:
    """A fake ledger: each `latest` moves on one ledger and shows the next scripted set of bot offers."""

    def __init__(self, script, outcomes):
        self.script, self.outcomes = list(script), list(outcomes)
        self.index, self.close = 1000, 500
        self.sent, self.current = [], ()
        self.client = SimpleNamespace(history=self.history, validated_index=lambda: self.index)
        self.clock_now = 0.0

    def latest(self):
        self.index += 1
        self.close += 4
        self.current = self.script.pop(0) if self.script else ()
        return SimpleNamespace(index=self.index, close_time=self.close)

    def account(self, address, index):
        return SimpleNamespace(offers=self.current if address == BOT else (), sequence=50 + len(self.sent))

    def submit(self, signed):
        self.sent.append(signed)
        return {"engine_result": "tesSUCCESS"}

    def history(self, method, params):
        meta = self.outcomes.pop(0)
        if meta is None:
            raise XrplReadError("txnNotFound")
        return SimpleNamespace(result={"validated": True, "meta": meta})

    def sleep(self, _):
        self.clock_now += 1
        self.index += 1

    def clock(self):
        self.clock_now += 0.1
        return self.clock_now


def run(w, price=None, attempts=6, side="sell"):
    return t.run(SEED, CFG, w, w.submit, TAKER, side, D(5), BOT, price, attempts, sleep=w.sleep, clock=w.clock)


KILLED = {"TransactionResult": "tecKILLED", "AffectedNodes": []}
HIT = {"TransactionResult": "tesSUCCESS", "AffectedNodes": [offer_node(BOT, 1, rl("6"), rl("5.6"), "75000000", "70000000")]}
AMM = {"TransactionResult": "tesSUCCESS", "AffectedNodes": [amm_node("1000000000", "1005000000")]}


def test_it_sends_through_the_live_bid_and_stops_on_a_hit(capsys):
    w = World([(bid(exp=10_000),)], [HIT])
    assert run(w) == 0
    tx = decode(w.sent[0].blob)
    assert D(tx["TakerPays"]["value"]) == D(5) * D("0.079954") and tx["Flags"] & 0x00020000
    assert "HIT THE BOT'S OFFER" in capsys.readouterr().out


def test_it_doesnt_send_while_the_only_bid_has_expired():
    w = World([(bid(exp=1),), (bid(exp=1),), (bid(exp=10_000),)], [HIT])
    assert run(w) == 0 and len(w.sent) == 1


def test_a_kill_is_tried_again_on_a_later_offer_up_to_the_limit(capsys):
    w = World([(bid(exp=10_000),)] * 5, [KILLED] * 3)
    assert run(w, attempts=3) == 1 and len(w.sent) == 3
    assert "wasn't hit in 3 attempt(s)" in capsys.readouterr().out


def test_a_fill_elsewhere_stops_it_rather_than_spending_more(capsys):
    w = World([(bid(exp=10_000),)] * 5, [AMM])
    assert run(w) == 1 and len(w.sent) == 1
    out = capsys.readouterr().out
    assert "hit the AMM" in out and "not with the bot" in out


def test_a_fixed_price_is_sent_once_at_that_price():
    w = World([()], [KILLED])
    assert run(w, price=D("0.07"), attempts=6) == 1 and len(w.sent) == 1
    assert D(decode(w.sent[0].blob)["TakerPays"]["value"]) == D("0.35")


def test_it_gives_up_when_no_offer_appears(capsys):
    w = World([], [])
    assert run(w) == 1 and not w.sent
    assert "no live bot offer" in capsys.readouterr().out


def test_a_transaction_that_never_lands_counts_as_nothing_happening(capsys):
    w = World([(bid(exp=10_000),)], [None, None, None, None, None, HIT])
    assert run(w, attempts=1) == 1
    assert "never reached a validated ledger" in capsys.readouterr().out


# --- a resting offer, for the daily-loss test --------------------------------------------------

RESTED = {"TransactionResult": "tesSUCCESS", "AffectedNodes": []}


def rest(w, price="0.0801", side="sell", minutes=10):
    return t.rest(SEED, CFG, w, w.submit, TAKER, side, D(1), BOT, D(price), minutes, sleep=w.sleep)


def test_a_resting_offer_is_passive_and_expires_by_itself(capsys):
    w = World([(bid(exp=10_000),)], [RESTED])
    assert rest(w) == 0
    tx = decode(w.sent[0].blob)
    assert tx["Flags"] & 0x00010000 and not tx["Flags"] & 0x00020000      # passive, not immediate-or-cancel
    assert tx["Expiration"] == w.close + 600
    assert "resting" in capsys.readouterr().out


def test_a_resting_offer_that_would_trade_with_the_bot_is_refused(capsys):
    w = World([(bid(exp=10_000),)], [])
    assert rest(w, price="0.0799") == 1 and w.sent == []
    assert "would trade with the bot's live offer" in capsys.readouterr().out
    w = World([(ask(exp=10_000),)], [])
    assert rest(w, price="0.0802", side="buy") == 1 and w.sent == []


def test_a_bot_offer_already_expired_doesnt_block_a_resting_offer():
    w = World([(bid(exp=100),)], [RESTED])
    assert rest(w, price="0.0799") == 0 and len(w.sent) == 1


def test_a_resting_offer_that_traded_isnt_reported_as_resting(capsys):
    w = World([()], [AMM])
    assert rest(w) == 1
    assert "isn't simply resting" in capsys.readouterr().out


def test_rest_needs_a_price():
    import pytest
    with pytest.raises(SystemExit):
        t.main(["--key-file", "x", "--bot-account", BOT, "--side", "sell", "--size", "1", "--rest"])
