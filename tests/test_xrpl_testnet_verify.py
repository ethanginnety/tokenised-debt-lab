"""M8 step 1: the testnet facts, the self-expiry rule, and what xrpl-py does when signing.

Offline. The saved files are read-only probes of testnet. The signing facts use a throwaway
key made in memory for the test: it is never stored, printed or sent anywhere.
"""

import json
from decimal import Decimal as D
from pathlib import Path

import pytest
from xrpl.core.binarycodec import decode
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import OfferCreate, OfferCreateFlag
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from debtlab.xrpl_market import closetimes, load_config, verify

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "real" / "xrpl_testnet" / "2026-11-02"
TEST = load_config(ROOT / "config" / "xrpl_testnet.json")
MAIN = load_config(ROOT / "config" / "xrpl.json")
EXPIRY = TEST["offer_expiry"]
FLAG_NO_CONSENSUS = 1


def res(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["result"]


def test_the_saved_probe_is_testnet_and_the_issuer_is_as_published():
    assert res("server_info")["info"]["network_id"] == TEST["network_id"] == 1
    s = verify.issuer_settings(res("issuer_account_info"))
    assert s.address == TEST["rlusd"]["issuer"] == "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"
    assert s.transfer_fee_pct == 0
    assert verify.obligations(res("issuer_gateway_balances"), TEST["rlusd"]["currency_hex"]) > 0
    n = verify.network_costs(res("server_state"))
    assert (n.reserve_base_xrp, n.reserve_inc_xrp) == (D(1), D("0.2"))


def test_the_testnet_book_price_is_nothing_like_mainnet():
    amm = res("amm_info")["amm"]
    price = D(amm["amount2"]["value"]) / (D(amm["amount"]) / 1_000_000)
    assert price < D("0.2")     # about 0.08 RLUSD per XRP, mainnet about 1.25: testnet says nothing about profit


def test_the_configs_cannot_be_confused():
    assert TEST["network"] == "testnet" and MAIN["network"] == "mainnet"
    assert not set(TEST["endpoints"]["websocket"] + TEST["endpoints"]["json_rpc"]) & set(
        MAIN["endpoints"]["websocket"] + MAIN["endpoints"]["json_rpc"])
    assert MAIN["rlusd"]["issuer"] in TEST["refused_issuers"]
    assert set(MAIN["known_lookalikes"]) <= set(TEST["refused_issuers"])
    assert TEST["rlusd"]["issuer"] not in TEST["refused_issuers"]


def test_saved_ledgers_are_the_kind_the_budget_was_measured_on():
    for h in json.loads((FIX / "ledger_headers.json").read_text(encoding="utf-8")):
        assert closetimes.budget_holds(h["close_time_resolution"], h["close_flags"], EXPIRY)


def test_the_budget_stops_holding_on_a_coarser_resolution_or_no_consensus():
    assert closetimes.budget_holds(10, 0, EXPIRY)
    assert not closetimes.budget_holds(20, 0, EXPIRY)
    assert not closetimes.budget_holds(10, FLAG_NO_CONSENSUS, EXPIRY)


@pytest.mark.parametrize("close", range(800_000_000, 800_000_020))
def test_an_expiration_is_a_boundary_ahead_of_the_close_time_and_within_budget(close):
    e = closetimes.expiration_for(close, EXPIRY)
    assert e % 10 == 0
    assert close + 35 < e <= close + 45


def test_the_worst_case_stays_within_60_seconds():
    worst = closetimes.worst_case_live_seconds(EXPIRY)
    assert worst == D("56.93") and worst <= D(EXPIRY["max_live_seconds"]) == D(60)


def test_a_refresh_is_due_only_near_expiry_and_leaves_time_to_land():
    assert not closetimes.refresh_due(1000, 1021, EXPIRY)
    assert closetimes.refresh_due(1000, 1020, EXPIRY)
    assert closetimes.refresh_due(1000, 990, EXPIRY)
    # a replacement sent when due is still ahead of the old offer by several ledgers
    assert EXPIRY["refresh_before_expiry_seconds"] >= 3 * 4


def test_boundary_offsets_take_the_first_ledger_to_reach_the_boundary():
    rows = [(1, 100, 100.5), (2, 101, 104.0), (3, 110, 108.0), (4, 111, 112.0), (5, 120, 117.5)]
    assert closetimes.boundary_offsets(rows) == [-2.0, -2.5]


def test_boundary_offsets_skip_a_break_in_the_ledgers():
    assert closetimes.boundary_offsets([(1, 100, 100.0), (9, 130, 131.0), (10, 131, 135.0)]) == []


def test_a_ledger_crossing_two_boundaries_counts_for_both():
    assert closetimes.boundary_offsets([(1, 101, 101.0), (2, 120, 125.0)]) == [15.0, 5.0]


@pytest.fixture
def signed():
    w = Wallet.create()
    tx = OfferCreate(account=w.address, sequence=100, fee="10", last_ledger_sequence=1000, expiration=800_000_000,
                     offer_sequence=99, flags=OfferCreateFlag.TF_PASSIVE, taker_gets="1000000",
                     taker_pays=IssuedCurrencyAmount(currency=TEST["rlusd"]["currency_hex"],
                                                     issuer=TEST["rlusd"]["issuer"], value="1"))
    return w, decode(sign(tx, w).blob())


def test_xrpl_py_keeps_the_seed_out_of_str_and_repr_but_not_vars(signed):
    w, _ = signed
    assert w.seed not in str(w) and w.seed not in repr(w) and w.private_key not in repr(w)
    assert w.seed in str(vars(w))    # so the signer must hide its wallet and never log its attributes


def test_a_signed_offer_names_no_network_so_only_key_separation_stops_a_replay(signed):
    _, tx = signed
    assert "NetworkID" not in tx
    assert (tx["Flags"], tx["OfferSequence"], tx["Expiration"], tx["LastLedgerSequence"]) == (
        OfferCreateFlag.TF_PASSIVE, 99, 800_000_000, 1000)


def test_signing_does_not_stop_a_nonsense_pair(signed):
    w, _ = signed
    tx = OfferCreate(account=w.address, sequence=1, fee="10", taker_gets="1", taker_pays="2")
    assert decode(sign(tx, w).blob())["TakerPays"] == "2"   # XRP for XRP: our own code must check the pair
