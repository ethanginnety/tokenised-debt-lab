"""M8: moving between testnet endpoints. Fake transports: nothing touches a network."""

import json
from functools import partial

import httpx
import pytest
from liveworld import CFG, FakeNet, rpc_transport, write_key

from debtlab.xrpl_market import load_config
from debtlab.xrpl_market.live import guards
from debtlab.xrpl_market.live import ledger as ledger_module
from debtlab.xrpl_market.live.guards import MainnetBlocked
from debtlab.xrpl_market.live.ledger import FailoverReads, TestnetLedger
from debtlab.xrpl_market.live.signer import Submitter, SubmitFailed, open_signer
from debtlab.xrpl_market.readonly import NotReadOnly, ReadOnlyClient, XrplReadError

PRIMARY, CLIO = "testnet.xrpl-labs.com", "clio.altnet.rippletest.net"
HTTPS = [f"https://{PRIMARY}/", f"https://{CLIO}/"]
WSS = [f"wss://{PRIMARY}/", f"wss://{CLIO}/"]


@pytest.fixture
def world(monkeypatch):
    net, down = FakeNet(), set()
    monkeypatch.setattr(ledger_module, "ReadOnlyClient", partial(ReadOnlyClient, transport=rpc_transport(net, down_hosts=down)))
    return net, down


def test_the_config_gives_the_bot_both_endpoints_and_the_monitor_one():
    assert CFG["live"]["endpoints"]["json_rpc"] == HTTPS and CFG["live"]["endpoints"]["websocket"] == WSS
    assert CFG["endpoints"]["json_rpc"] == HTTPS[:1] and CFG["endpoints"]["websocket"] == WSS[:1]


def test_reads_use_the_first_endpoint_while_it_answers(world):
    net, _ = world
    assert FailoverReads(HTTPS).validated_index() == net.index and set(net.read_hosts) == {PRIMARY}


def test_reads_move_to_the_second_and_stay_there(world):
    net, down = world
    r = FailoverReads(HTTPS)
    r.validated_index()
    down.add(PRIMARY)
    assert r.validated_index() == net.index and r.url == HTTPS[1]
    down.clear()
    net.read_hosts.clear()
    r.validated_index()
    assert set(net.read_hosts) == {CLIO}


def test_reads_move_back_when_the_second_fails_too(world):
    net, down = world
    r = FailoverReads(HTTPS)
    down.add(PRIMARY)
    r.validated_index()
    down.clear()
    down.add(CLIO)
    r.validated_index()
    assert r.url == HTTPS[0]


def test_with_every_endpoint_down_a_read_fails(world):
    _, down = world
    down |= {PRIMARY, CLIO}
    with pytest.raises(XrplReadError):
        FailoverReads(HTTPS).validated_index()


def test_another_network_raises_and_does_not_fail_over(world):
    net, _ = world
    net.network_id = 0
    with pytest.raises(MainnetBlocked):
        FailoverReads(HTTPS).validated_index()


def test_a_write_method_is_refused_and_doesnt_try_the_next(world):
    net, _ = world
    with pytest.raises(NotReadOnly):
        FailoverReads(HTTPS).history("submit", {})
    assert net.sent == []


def test_a_reply_that_isnt_validated_counts_as_a_failed_read(monkeypatch):
    """Clio's real book_offers reply, 2 Nov: no `validated` field. The check stays."""
    def handler(request):
        return httpx.Response(200, json={"result": {"status": "success", "offers": [], "ledger_index": 5, "ledger_hash": "A"}})
    with ReadOnlyClient(HTTPS[1], transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(XrplReadError, match="not validated"):
            c.at("book_offers", {}, 5)


@pytest.mark.parametrize("url", ["wss://clio.altnet.rippletest.net.evil.example/", "wss://evil.example/clio.altnet.rippletest.net",
                                 "wss://clio.altnet.rippletest.net@evil.example/", "ws://clio.altnet.rippletest.net/",
                                 "wss://clio.devnet.rippletest.net/", "wss://clio.altnet.rippletest.net:8443/"])
def test_look_alikes_of_the_new_host_are_refused(url):
    with pytest.raises(MainnetBlocked):
        guards.check_endpoint(url)
    with pytest.raises(MainnetBlocked):
        FailoverReads([HTTPS[0], url])
    with pytest.raises(MainnetBlocked):
        Submitter(WSS[0], url, connect=FakeNet())


def test_a_mainnet_endpoint_is_refused_in_the_lists():
    main = load_config()["endpoints"]
    with pytest.raises(MainnetBlocked):
        FailoverReads(main["json_rpc"])
    with pytest.raises(MainnetBlocked):
        Submitter(main["websocket"][0], connect=FakeNet())
    bad = {**CFG, "live": {"endpoints": {"websocket": main["websocket"], "json_rpc": []}}}
    with pytest.raises(MainnetBlocked):
        guards.check_config(bad)
    with pytest.raises(MainnetBlocked):
        TestnetLedger(bad)


# --- sending ---------------------------------------------------------------------------------

@pytest.fixture
def signed(tmp_path):
    s = open_signer(write_key(tmp_path), CFG, on_mainnet=lambda a: False, acl=lambda p: [], me=lambda: "me")
    return s.cancel_offer(offer_sequence=5, sequence=100, last_ledger=1003, fee_drops=10)


def submits(net):
    return [json.loads(json.dumps(m)) for m in net.sent if m["command"] == "submit"]


def test_sends_use_the_first_endpoint_while_it_answers(signed):
    net = FakeNet()
    assert Submitter(*WSS, connect=net).submit(signed)["engine_result"] == "tesSUCCESS"
    assert set(net.urls) == {WSS[0]}


def test_sends_move_on_if_the_first_doesnt_answer_before_sending(signed):
    net = FakeNet()
    net.down_urls.add(WSS[0])
    sub = Submitter(*WSS, connect=net)
    assert sub.submit(signed)["engine_result"] == "tesSUCCESS"
    assert set(net.urls) == {WSS[1]} and sub.url == WSS[1] and len(submits(net)) == 1


def test_a_signed_transaction_is_never_resent_to_the_next_endpoint(signed):
    net = FakeNet()
    net.no_answer_to_submit = True
    sub = Submitter(*WSS, connect=net)
    with pytest.raises(SubmitFailed) as e:
        sub.submit(signed)
    assert e.value.sent is True and len(submits(net)) == 1      # it may have arrived: outcome unknown
    assert sub.url == WSS[1]                                    # only what comes next moves on


def test_with_every_endpoint_down_nothing_is_sent(signed):
    net = FakeNet()
    net.down_urls |= set(WSS)
    with pytest.raises(SubmitFailed) as e:
        Submitter(*WSS, connect=net).submit(signed)
    assert e.value.sent is False and submits(net) == []


def test_every_endpoint_is_checked_before_a_submit(signed):
    net = FakeNet()
    net.down_urls.add(WSS[0])
    net.network_id = 0
    with pytest.raises(MainnetBlocked):
        Submitter(*WSS, connect=net).submit(signed)
    assert submits(net) == []


def test_alive_finds_whichever_answers():
    net = FakeNet()
    sub = Submitter(*WSS, connect=net)
    net.down_urls.add(WSS[0])
    assert sub.alive() and sub.url == WSS[1]
    net.down_urls |= set(WSS)
    sub.close()
    assert not sub.alive()
