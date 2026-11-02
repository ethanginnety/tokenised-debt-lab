"""The two paper fill rules of decision E (pure functions)."""

from decimal import Decimal as D

import pytest

from debtlab.xrpl_market.paper.fills import Trade, fill, qualifies


def t(side, price, xrp, venue="book", n=1):
    return Trade(100, f"H{n}", f"N{n}", venue, side, D(price), D(xrp))


@pytest.mark.parametrize("rule, trade_price, fills", [
    ("conservative", "1.2490", True), ("conservative", "1.2500", False), ("conservative", "1.2510", False),
    ("optimistic", "1.2490", True), ("optimistic", "1.2500", True), ("optimistic", "1.2510", False),
])
def test_our_bid_is_hit_by_takers_selling_at_or_below_it(rule, trade_price, fills):
    assert qualifies("buy", D("1.25"), t("sell_xrp", trade_price, 5), rule) is fills


@pytest.mark.parametrize("rule, trade_price, fills", [
    ("conservative", "1.2510", True), ("conservative", "1.2500", False), ("conservative", "1.2490", False),
    ("optimistic", "1.2510", True), ("optimistic", "1.2500", True), ("optimistic", "1.2490", False),
])
def test_our_offer_is_lifted_by_takers_buying_at_or_above_it(rule, trade_price, fills):
    assert qualifies("sell", D("1.25"), t("buy_xrp", trade_price, 5), rule) is fills


def test_the_wrong_direction_never_fills():
    for rule in ("conservative", "optimistic"):
        assert not qualifies("buy", D("1.25"), t("buy_xrp", "1.20", 5), rule)      # takers buying don't hit bids
        assert not qualifies("sell", D("1.25"), t("sell_xrp", "1.30", 5), rule)


def test_amm_trades_count_under_both_rules():
    amm = t("sell_xrp", "1.2400", 7, venue="amm")
    assert fill("buy", D("1.25"), D(9), [amm], "conservative")[0] == D(7)
    assert fill("buy", D("1.25"), D(9), [amm], "optimistic")[0] == D(7)


def test_volume_is_capped_by_the_trades_and_by_what_is_left():
    trades = [t("sell_xrp", "1.24", 3, n=1), t("sell_xrp", "1.26", 50, n=2), t("sell_xrp", "1.245", 4, n=3)]
    qty, used = fill("buy", D("1.25"), D(9), trades, "conservative")
    assert qty == D(7) and [u["node"] for u in used] == ["N1", "N3"]           # the 1.26 trade is above our bid
    qty, used = fill("buy", D("1.25"), D(5), trades, "conservative")
    assert qty == D(5) and used[-1]["xrp"] == "2"                                # only what is left of the order


def test_fills_round_down_to_whole_drops_and_nothing_is_nothing():
    assert fill("buy", D("1.25"), D(9), [t("sell_xrp", "1.24", "0.0000009")], "optimistic") == (D(0), [])
    assert fill("buy", D("1.25"), D(9), [t("sell_xrp", "1.24", "1.2345678")], "optimistic")[0] == D("1.234567")
    assert fill("buy", D("1.25"), D(9), [], "optimistic") == (D(0), [])


def test_unknown_rule_or_side_is_an_error():
    with pytest.raises(ValueError):
        qualifies("buy", D(1), t("sell_xrp", 1, 1), "hopeful")
    with pytest.raises(ValueError):
        qualifies("hold", D(1), t("sell_xrp", 1, 1), "optimistic")
