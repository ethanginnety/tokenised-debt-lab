"""Trades from transaction metadata (M7 step 3): hand-made cases and real validated transactions."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from debtlab.xrpl_market import load_config, trades

CFG = load_config()
RLUSD, ISSUER = CFG["rlusd"]["currency_hex"], CFG["rlusd"]["issuer"]
AMM = "rhWTXC2m2gGGA9WozUaoMm6kLAVPb1tcS3"
LOOKALIKE = next(iter(CFG["known_lookalikes"]))
REAL = Path(__file__).parent / "fixtures" / "real" / "xrpl" / "tx_2026-11-01"


def rl(v, issuer=ISSUER):
    return {"currency": RLUSD, "issuer": issuer, "value": str(v)}


def drops(x):
    return str(int(Decimal(str(x)) * 1_000_000))


def msg(nodes, ttype="OfferCreate", result="tesSUCCESS", validated=True):
    return {"type": "transaction", "validated": validated, "ledger_index": 100,
            "transaction": {"TransactionType": ttype, "hash": "H" * 64},
            "meta": {"TransactionResult": result, "AffectedNodes": nodes}}


def offer(kind, gets0, pays0, gets1, pays1, idx="OFF1", owner="rMaker"):
    return {kind: {"LedgerEntryType": "Offer", "LedgerIndex": idx,
                   "PreviousFields": {"TakerGets": gets0, "TakerPays": pays0},
                   "FinalFields": {"Account": owner, "TakerGets": gets1, "TakerPays": pays1}}}


def amm_nodes(x0, x1, r0, r1):
    return [{"ModifiedNode": {"LedgerEntryType": "AccountRoot",
                              "PreviousFields": {"Balance": drops(x0)}, "FinalFields": {"Account": AMM, "Balance": drops(x1)}}},
            {"ModifiedNode": {"LedgerEntryType": "RippleState",
                              "PreviousFields": {"Balance": {"currency": RLUSD, "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji", "value": str(-r0)}},
                              "FinalFields": {"Balance": {"currency": RLUSD, "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji", "value": str(-r1)},
                                              "HighLimit": {"issuer": AMM}, "LowLimit": {"issuer": ISSUER}}}}]


def ex(m):
    return trades.extract(m, RLUSD, ISSUER, AMM)


def test_bid_consumed_means_the_taker_sold_xrp():
    # bid: maker gives 125 RLUSD for 100 XRP; 40 XRP of it taken
    (t,) = ex(msg([offer("ModifiedNode", rl(125), drops(100), rl(75), drops(60))]))
    assert (t.venue, t.taker_side, t.xrp, t.rlusd, t.price, t.maker, t.node) == \
        ("book", "sell_xrp", 40, 50, Decimal("1.25"), "rMaker", "OFF1")


def test_ask_fully_consumed_means_the_taker_bought_xrp():
    (t,) = ex(msg([offer("DeletedNode", drops(10), rl("12.6"), "0", rl(0))], ttype="Payment"))
    assert (t.taker_side, t.xrp, t.rlusd, t.price, t.tx_type) == ("buy_xrp", 10, Decimal("12.6"), Decimal("1.26"), "Payment")


def test_not_trades():
    cancelled = {"DeletedNode": {"LedgerEntryType": "Offer", "LedgerIndex": "X",
                                 "FinalFields": {"Account": "rM", "TakerGets": drops(1), "TakerPays": rl(1)}}}
    assert ex(msg([cancelled], ttype="OfferCancel")) == []
    assert ex(msg([offer("ModifiedNode", rl(125), drops(100), rl(75), drops(60))], result="tecKILLED")) == []
    other = {"currency": "USD", "issuer": "rOther", "value": "5"}
    assert ex(msg([offer("ModifiedNode", other, drops(10), {**other, "value": "1"}, drops(2))])) == []
    fake = offer("ModifiedNode", rl(125, LOOKALIKE), drops(100), rl(75, LOOKALIKE), drops(60))
    assert ex(msg([fake])) == []                     # the look-alike issuer's "RLUSD" is not RLUSD
    with pytest.raises(trades.NotValidated):
        ex(msg([], validated=False))


def test_amm_swaps_both_ways():
    (s,) = ex(msg(amm_nodes(1000, 1010, 1250, Decimal("1237.6")), ttype="Payment"))   # pool gained XRP
    assert (s.venue, s.taker_side, s.xrp, s.rlusd, s.maker) == ("amm", "sell_xrp", 10, Decimal("12.4"), AMM)
    assert s.pool_price_before == Decimal("1.25") and s.pool_price_after == Decimal("1237.6") / 1010
    (b,) = ex(msg(amm_nodes(1000, 990, 1250, Decimal("1262.6"))))
    assert (b.taker_side, b.xrp, b.rlusd) == ("buy_xrp", 10, Decimal("12.6"))


def test_amm_deposit_is_not_a_swap():
    assert ex(msg(amm_nodes(1000, 990, 1250, 1262), ttype="AMMDeposit")) == []
    assert ex(msg(amm_nodes(1000, 1010, 1250, 1262))) == []          # both up: not a swap either


def test_book_and_amm_in_one_transaction():
    nodes = [offer("ModifiedNode", rl(125), drops(100), rl(75), drops(60)), *amm_nodes(1000, 1010, 1250, Decimal("1237.6"))]
    assert [(t.venue, t.taker_side) for t in ex(msg(nodes))] == [("book", "sell_xrp"), ("amm", "sell_xrp")]


# --- real validated transactions from the 1 Nov smoke run --------------------------------

EXPECTED = {
    "15341980CF67": [("book", "buy_xrp")],
    "21F997648477": [("book", "sell_xrp")] * 2 + [("amm", "sell_xrp")],
    "4C1749885593": [("book", "sell_xrp")],
    "8429DA17BCE2": [("amm", "sell_xrp")],
    "A107D88F5762": [("book", "sell_xrp")] * 3 + [("amm", "sell_xrp")],
    "A901EF6DB4BE": [("book", "sell_xrp")] * 5 + [("amm", "sell_xrp")],
    "DF01869D84B2": [("amm", "sell_xrp")],
}
FILES = sorted(REAL.glob("tx_*.json"))


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.mark.skipif(not FILES, reason="no real transactions saved")
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name[3:15])
def test_real_transaction(path):
    m = load(path)
    found = ex(m)
    assert [(t.venue, t.taker_side) for t in found] == EXPECTED[path.name[3:15]]
    assert all(t.xrp > 0 and t.rlusd > 0 and Decimal("1.1") < t.price < Decimal("1.4") for t in found)
    assert {t.tx_hash for t in found} == {m["transaction"]["hash"]} and {t.ledger_index for t in found} == {m["ledger_index"]}


@pytest.mark.skipif(not FILES, reason="no real transactions saved")
def test_real_amm_swaps_follow_xls30():
    """A real swap matches XLS-30 eq. 9 with the pool's 0.214% fee to within 2e-7 (rippled's
    rounding, in the pool's favour), and the taker gets about the fee less than the pool price."""
    fee = Decimal("0.00214")
    swaps = [t for p in FILES for t in ex(load(p)) if t.venue == "amm"]
    assert len(swaps) == 5
    for t in swaps:
        x, r = t.pool_xrp_before, t.pool_rlusd_before
        predicted = r * (1 - x / (x + t.xrp * (1 - fee)))
        assert abs(t.rlusd - predicted) / t.rlusd < Decimal("2e-7")
        assert t.rlusd <= predicted                                     # rounding never favours the taker
        assert (1 - t.price / t.pool_price_before) > fee * Decimal("0.99")
