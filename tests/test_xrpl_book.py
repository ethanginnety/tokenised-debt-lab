"""M7 step 2: the RLUSD/XRP book adapter, on hand-made books and the real 1 Nov snapshot."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from debtlab.xrpl_market import book, load_config, verify

CFG = load_config()
RLUSD = CFG["rlusd"]["currency_hex"]
ISSUER = CFG["rlusd"]["issuer"]
REAL = Path(__file__).parent / "fixtures" / "real" / "xrpl" / "2026-11-01"


def rl(v):
    return {"currency": RLUSD, "issuer": ISSUER, "value": str(v)}


def drops(xrp):
    return str(int(Decimal(str(xrp)) * 1_000_000))


def bid(price, xrp, funded_xrp=None, i="b"):
    """An offer to buy `xrp` XRP at `price` RLUSD each."""
    o = {"TakerGets": rl(Decimal(str(price)) * Decimal(str(xrp))), "TakerPays": drops(xrp), "Account": "rB", "index": i}
    if funded_xrp is not None:
        o["taker_pays_funded"] = drops(funded_xrp)
        o["taker_gets_funded"] = rl(Decimal(str(price)) * Decimal(str(funded_xrp)))
    return o


def ask(price, xrp, i="a"):
    return {"TakerGets": drops(xrp), "TakerPays": rl(Decimal(str(price)) * Decimal(str(xrp))), "Account": "rA", "index": i}


def page(offers, ledger=10, **extra):
    return {"ledger_index": ledger, "validated": True, "offers": offers, **extra}


def simple():
    # mid 1.25; bids 1.249 (100 XRP), 1.245 (200), 1.20 (1000); asks 1.251 (100), 1.255 (200), 1.30 (1000)
    return book.parse_book([page([bid(1.249, 100), bid(1.245, 200), bid(1.20, 1000)])],
                           [page([ask(1.251, 100), ask(1.255, 200), ask(1.30, 1000)])], RLUSD)


def test_sides_sorted_best_first_with_exact_prices():
    b = simple()
    assert [lv.price for lv in b.bids] == [Decimal("1.249"), Decimal("1.245"), Decimal("1.2")]
    assert [lv.price for lv in b.asks] == [Decimal("1.251"), Decimal("1.255"), Decimal("1.3")]
    assert b.bids[0].rlusd == Decimal("124.9") and b.asks[0].xrp == 100


def test_stats_hand_example():
    s = book.stats(simple())
    assert s.valid and s.mid == Decimal("1.25") and s.spread_bp == Decimal("16")
    d = {x.band_bp: x for x in s.depth}
    assert (d[10].bid_xrp, d[10].ask_xrp) == (100, 100)          # 1.24875 .. 1.25125
    assert (d[25].bid_xrp, d[25].ask_xrp) == (100, 100)          # 1.246875 .. 1.253125
    assert (d[50].bid_xrp, d[50].ask_xrp) == (300, 300)          # 1.24375 .. 1.25625
    assert d[50].bid_rlusd == Decimal("124.9") + Decimal("249.0")
    assert d[100].ask_rlusd == Decimal("125.1") + Decimal("251.0")
    assert all(x.complete for x in s.depth)


def test_partly_funded_offer_keeps_its_price_and_trades_only_its_funding():
    b = book.parse_book([page([bid(1.249, 100, funded_xrp=40)])], [page([ask(1.251, 10)])], RLUSD)
    assert b.bids[0].price == Decimal("1.249") and b.bids[0].xrp == 40 and b.bids[0].rlusd == Decimal("49.96")


def test_unfunded_and_dust_offers_dropped():
    dust = bid(1.249, 100)
    dust["taker_gets_funded"], dust["taker_pays_funded"] = rl("0.0000003"), "0"
    zero = bid(1.248, 100, funded_xrp=0)
    b = book.parse_book([page([dust, zero, bid(1.24, 10)])], [page([ask(1.26, 10)])], RLUSD)
    assert len(b.bids) == 1 and b.dropped == 2


@pytest.mark.parametrize("bids,asks,why", [
    ([], [ask(1.26, 1)], "no funded bids"),
    ([bid(1.24, 1)], [], "no funded asks"),
    ([bid(1.26, 1)], [ask(1.25, 1)], "crossed"),
    ([bid(1.25, 1)], [ask(1.25, 1)], "locked"),
])
def test_invalid_books(bids, asks, why):
    s = book.stats(book.parse_book([page(bids)], [page(asks)], RLUSD))
    assert not s.valid and why in " ".join(s.reasons) and s.mid is None


def test_refuses_mixed_ledgers_unvalidated_and_wrong_side():
    with pytest.raises(ValueError, match="different ledgers"):
        book.parse_book([page([bid(1.2, 1)], 10)], [page([ask(1.3, 1)], 11)], RLUSD)
    with pytest.raises(ValueError, match="validated"):
        book.parse_book([page([bid(1.2, 1)], validated=False)], [page([ask(1.3, 1)])], RLUSD)
    with pytest.raises(ValueError, match="ask offer in the bid"):
        book.parse_book([page([ask(1.3, 1)])], [page([ask(1.3, 1)])], RLUSD)


def test_cut_off_side_marks_only_the_bands_it_reaches():
    # bids read stop at 1.245 (a full page): bands beyond it are lower bounds
    b = book.parse_book([page([bid(1.249, 100), bid(1.245, 200)])], [page([ask(1.251, 100)])],
                        RLUSD, page_limit=2)
    assert not b.bids_complete and b.asks_complete
    d = {x.band_bp: x.complete for x in book.stats(b).depth}
    assert d == {10: True, 25: True, 50: False, 100: False}   # last bid 1.245 < 1.246875 but not < 1.24375


def test_walk_buy_and_sell():
    b = simple()
    w = book.walk(b, "buy", Decimal("200"), Decimal("1.25"))
    # 125.1 RLUSD buys 100 XRP at 1.251, then 74.9 at 1.255
    assert w.filled and w.levels == 2 and w.worst_price == Decimal("1.255")
    assert w.xrp == 100 + Decimal("74.9") / Decimal("1.255")
    assert w.avg_price == Decimal(200) / w.xrp and w.cost_bp > 0
    s = book.walk(b, "sell", Decimal("10"), Decimal("1.25"))
    assert s.levels == 1 and s.avg_price == Decimal("1.249") and s.cost_bp == Decimal("8")
    big = book.walk(b, "sell", Decimal("100000"), Decimal("1.25"))
    assert not big.filled and big.rlusd == Decimal("124.9") + Decimal("249.0") + Decimal("1200")
    with pytest.raises(ValueError):
        book.walk(b, "hold", Decimal(1))


def pool(x=1000, r=1250, fee=0):
    return verify.Amm(10, "rAMM", Decimal(x), Decimal(r), Decimal(r) / Decimal(x), Decimal(fee) / 1000)


def test_amm_constant_product_without_fee():
    w = book.amm_walk(pool(), "buy", Decimal(125))
    assert w.xrp == Decimal(1000) * (1 - Decimal(1250) / Decimal(1375))           # 90.909...
    assert (Decimal(1000) - w.xrp) * Decimal(1375) == pytest.approx(Decimal(1250000), rel=Decimal("1e-30"))
    s = book.amm_walk(pool(), "sell", Decimal(125))
    assert s.xrp == Decimal(1000) * (Decimal(1250) / Decimal(1125) - 1)              # 111.11...
    assert not book.amm_walk(pool(), "sell", Decimal(1250)).filled


def test_amm_fee_on_input():
    p = pool(fee=214)                                   # 0.214%
    assert book.amm_fee(p) == Decimal("0.00214")
    w = book.amm_walk(p, "buy", Decimal(10))
    # the pool keeps the product on the input net of fee: (X - out) * (R + 10 * (1 - fee)) = X * R
    assert (Decimal(1000) - w.xrp) * (Decimal(1250) + 10 * (1 - Decimal("0.00214"))) == pytest.approx(Decimal(1250000))
    # a tiny trade costs about the fee over the pool price, either way
    t = book.amm_walk(p, "buy", Decimal("0.0001"), Decimal("1.25"))
    assert t.cost_bp == pytest.approx(Decimal("21.4"), abs=Decimal("0.1"))
    t = book.amm_walk(p, "sell", Decimal("0.0001"), Decimal("1.25"))
    assert t.cost_bp == pytest.approx(Decimal("21.35"), abs=Decimal("0.1"))


# --- the real snapshot ------------------------------------------------------------------

def load(name):
    return [json.loads(p.read_text(encoding="utf-8"))["result"] for p in sorted(REAL.glob(f"book_{name}_*.json"))]


@pytest.mark.skipif(not REAL.exists(), reason="no real probe saved")
def test_real_book():
    b = book.parse_book(load("bids"), load("asks"), RLUSD, page_limit=400)
    s = book.stats(b)
    assert s.valid and s.ledger_index == 108053087
    assert not b.bids_complete and not b.asks_complete              # both pages full
    assert b.dropped == 38 and len(b.bids) == 362 and len(b.asks) == 400
    assert s.best_bid == pytest.approx(Decimal("1.248241"), abs=Decimal("1e-6"))
    assert s.best_ask == pytest.approx(Decimal("1.249899"), abs=Decimal("1e-6"))
    assert s.spread_bp == pytest.approx(Decimal("13.27"), abs=Decimal("0.01"))
    depth = [d.bid_rlusd for d in s.depth]
    assert depth == sorted(depth) and all(d.complete for d in s.depth)   # pages reach past 100 bp
    # the same best prices as step 1's first look
    v = verify.book_side({"ledger_index": 1, "offers": load("bids")[0]["offers"]}, RLUSD)
    assert s.best_bid == pytest.approx(v.best, rel=Decimal("1e-9"))
    w = book.walk(b, "buy", Decimal("11"), s.mid)
    assert w.filled and w.levels == 1 and Decimal(0) < w.cost_bp < s.spread_bp


@pytest.mark.skipif(not REAL.exists(), reason="no real probe saved")
def test_real_amm_beside_the_book():
    a = verify.amm(json.loads((REAL / "amm_info.json").read_text(encoding="utf-8"))["result"], RLUSD)
    s = book.stats(book.parse_book(load("bids"), load("asks"), RLUSD, page_limit=400))
    buy, sell = book.amm_walk(a, "buy", Decimal("11"), s.mid), book.amm_walk(a, "sell", Decimal("11"), s.mid)
    # with its fee the pool is worse than the book's best on both sides at this size
    assert buy.avg_price > s.best_ask and sell.avg_price < s.best_bid
