"""M7 step 1 checks, on hand-made cases and on every real probe saved by scripts/xrpl_probe.py."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from xrpl.core.addresscodec import is_valid_classic_address

from debtlab.xrpl_market import load_config, verify

CFG = load_config()
RLUSD = CFG["rlusd"]["currency_hex"]
ISSUER = CFG["rlusd"]["issuer"]
PROBES = sorted(p.parent for p in (Path(__file__).parent / "fixtures" / "real" / "xrpl").glob("*/manifest.json"))

TOML = """
[[ISSUERS]]
address = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
name = "RLUSD"
[[TOKENS]]
issuer = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
currency = "RLUSD"
name = "Ripple USD"
"""


def rl(v):
    return {"currency": RLUSD, "issuer": ISSUER, "value": str(v)}


def drops(xrp):
    return str(int(Decimal(xrp) * 1_000_000))


# --- issuer -----------------------------------------------------------------------------

def test_toml_listing_is_needed_not_just_a_domain():
    assert verify.listed_in_toml(TOML, ISSUER, "RLUSD")
    assert not verify.listed_in_toml(TOML, "rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK", "RLUSD")
    assert not verify.listed_in_toml(TOML, ISSUER, "USD")


def test_transfer_rate_to_percent():
    info = {"ledger_index": 5, "account_data": {"Account": "rA", "TransferRate": 1005000000, "Domain": "6162"}}
    s = verify.issuer_settings(info)
    assert s.transfer_fee_pct == Decimal("0.5") and s.domain == "ab" and s.tick_size is None
    assert verify.issuer_settings({"ledger_index": 5, "account_data": {"Account": "rA"}}).transfer_fee_pct == 0


def test_network_costs_from_drops_and_load():
    st = {"state": {"load_factor": 512, "load_base": 256,
                    "validated_ledger": {"seq": 9, "base_fee": 10, "reserve_base": 1000000, "reserve_inc": 200000}}}
    n = verify.network_costs(st)
    assert (n.reserve_base_xrp, n.reserve_inc_xrp, n.current_fee_drops) == (1, Decimal("0.2"), 20)


# --- book -------------------------------------------------------------------------------

def test_sides_prices_and_funding():
    bids = {"ledger_index": 7, "offers": [
        {"TakerGets": rl(125), "TakerPays": drops(100)},                                   # 1.25
        {"TakerGets": rl(124), "TakerPays": drops(100), "taker_gets_funded": rl(62),
         "taker_pays_funded": drops(50)},                                                   # half funded, 1.24
        {"TakerGets": rl(200), "TakerPays": drops(100), "taker_gets_funded": rl(0),
         "taker_pays_funded": "0"},                                                         # unfunded, ignored
    ]}
    asks = {"ledger_index": 7, "offers": [{"TakerGets": drops(10), "TakerPays": rl("12.6")}]}  # 1.26
    b, a = verify.book_side(bids, RLUSD), verify.book_side(asks, RLUSD)
    assert (b.side, b.funded_offers, b.offers, b.xrp, b.rlusd, b.best) == ("bid", 2, 3, 150, 187, Decimal("1.25"))
    assert (a.side, a.best, a.xrp) == ("ask", Decimal("1.26"), 10)
    assert verify.go_no_go(b, a, same_ledger=True).go


def test_no_go_cases():
    one = {"ledger_index": 7, "offers": [{"TakerGets": rl(125), "TakerPays": drops(100)}]}
    none = {"ledger_index": 7, "offers": []}
    b = verify.book_side(one, RLUSD)
    g = verify.go_no_go(b, verify.book_side(none, RLUSD), same_ledger=True)
    assert not g.go and any("no funded offers" in r for r in g.reasons)
    crossed = verify.book_side({"ledger_index": 7, "offers": [{"TakerGets": drops(10), "TakerPays": rl(12)}]}, RLUSD)
    assert any("crossed" in r for r in verify.go_no_go(b, crossed, same_ledger=True).reasons)
    assert not verify.go_no_go(b, verify.book_side(one, RLUSD), same_ledger=False).go


def test_full_page_without_marker_is_not_complete():
    page = {"ledger_index": 7, "offers": [{"TakerGets": rl(1), "TakerPays": drops(1)}] * 2, "last_page_size": 2}
    assert not verify.book_side(page, RLUSD, page_limit=2).complete
    assert verify.book_side(page, RLUSD, page_limit=3).complete
    assert not verify.book_side({**page, "marker": "m"}, RLUSD).complete


def test_other_pairs_are_refused():
    bad = {"ledger_index": 7, "offers": [{"TakerGets": {"currency": "USD", "issuer": "rB", "value": "1"},
                                          "TakerPays": drops(1)}]}
    with pytest.raises(ValueError):
        verify.book_side(bad, RLUSD)


def test_amm_fee_units():
    info = {"ledger_index": 7, "amm": {"account": "rAMM", "amount": drops(1000), "amount2": rl(1250), "trading_fee": 214}}
    a = verify.amm(info, RLUSD)
    assert (a.price, a.trading_fee_pct) == (Decimal("1.25"), Decimal("0.214"))


# --- real probes ------------------------------------------------------------------------

def res(folder, name):
    return json.loads((folder / name).read_text(encoding="utf-8"))["result"]


@pytest.mark.skipif(not PROBES, reason="no real probes saved yet")
@pytest.mark.parametrize("folder", PROBES, ids=lambda p: p.name)
def test_real_probe(folder):
    m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    toml = (folder / "ripple_xrp-ledger.toml").read_text(encoding="utf-8")
    seq = m["ledger_index"]

    s = verify.issuer_settings(res(folder, "issuer_account_info.json"))
    assert s.address == ISSUER and s.domain == "https://ripple.com/"
    assert verify.listed_in_toml(toml, ISSUER, "RLUSD")
    for acct in CFG["known_lookalikes"]:
        if acct in m["checked_accounts"]:
            assert not verify.listed_in_toml(toml, acct, "RLUSD")
    assert verify.obligations(res(folder, "issuer_gateway_balances.json"), RLUSD) > 0

    sides = {}
    for name in ("bids", "asks"):
        offers = []
        for p in sorted(folder.glob(f"book_{name}_*.json")):
            r = json.loads(p.read_text(encoding="utf-8"))["result"]
            assert r["validated"] is True and r["ledger_index"] == seq
            offers += r["offers"]
        sides[name] = verify.book_side({"ledger_index": seq, "offers": offers}, RLUSD)
    a = verify.amm(res(folder, "amm_info.json"), RLUSD)
    assert {s.ledger_index, a.ledger_index, verify.network_costs(res(folder, "server_state.json")).ledger_index} == {seq}
    assert (sides["bids"].side, sides["asks"].side) == ("bid", "ask")
    assert verify.go_no_go(sides["bids"], sides["asks"], same_ledger=True).go


def test_config_addresses_are_valid_and_distinct():
    addrs = [ISSUER, *CFG["known_lookalikes"]]
    assert all(is_valid_classic_address(a) for a in addrs)
    assert ISSUER not in CFG["known_lookalikes"]
    assert bytes.fromhex(RLUSD[:10]) == b"RLUSD" and set(RLUSD[10:]) == {"0"} and len(RLUSD) == 40
    assert CFG["network"] == "mainnet" and all(u.startswith(("https://", "wss://"))
                                               for v in CFG["endpoints"].values() for u in v)
