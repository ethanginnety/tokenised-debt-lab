"""The XRPL client can only read, only at a validated ledger, and checks what comes back."""

import json

import httpx
import pytest

from debtlab.xrpl_market.readonly import ALLOWED, NotReadOnly, ReadOnlyClient, XrplReadError

WRITE_METHODS = ["submit", "submit_multisigned", "sign", "sign_for", "wallet_propose",
                 "channel_authorize", "ledger_accept", "stop", "connect"]


def client(handler):
    sent = []

    def h(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"result": handler(body["method"], body["params"][0])})

    return ReadOnlyClient("https://example.invalid/", transport=httpx.MockTransport(h)), sent


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_and_signing_methods_are_refused_before_sending(method):
    c, sent = client(lambda m, p: {"status": "success"})
    with pytest.raises(NotReadOnly):
        c._post(method, {})
    with pytest.raises(NotReadOnly):
        c.at(method, {}, 1)
    assert sent == []


def test_allowed_methods_are_only_public_reads():
    assert ALLOWED == {"ledger", "book_offers", "amm_info", "account_info", "account_lines",
                       "gateway_balances", "server_state", "server_info", "subscribe", "unsubscribe"}


def test_subscribe_is_not_sent_over_json_rpc():
    c, sent = client(lambda m, p: {"status": "success"})
    with pytest.raises(NotReadOnly):
        c._post("subscribe", {})
    assert sent == []


def test_reads_pin_the_ledger_index():
    c, sent = client(lambda m, p: {"status": "success", "validated": True, "ledger_index": p["ledger_index"]})
    c.at("account_info", {"account": "rX"}, 100)
    assert sent[0]["params"][0] == {"account": "rX", "ledger_index": 100}


@pytest.mark.parametrize("index", ["validated", "current", "closed", None, True])
def test_ledger_shortcuts_are_refused(index):
    c, sent = client(lambda m, p: {"status": "success", "validated": True})
    with pytest.raises(XrplReadError):
        c.at("account_info", {"account": "rX"}, index)
    assert sent == []


def test_unvalidated_response_is_rejected():
    c, _ = client(lambda m, p: {"status": "success", "validated": False, "ledger_index": p["ledger_index"]})
    with pytest.raises(XrplReadError, match="not validated"):
        c.at("book_offers", {}, 100)


def test_response_from_another_ledger_is_rejected():
    c, _ = client(lambda m, p: {"status": "success", "validated": True, "ledger_index": 99})
    with pytest.raises(XrplReadError, match="asked for ledger 100"):
        c.at("book_offers", {}, 100)


def test_server_error_is_raised_not_swallowed():
    c, _ = client(lambda m, p: {"status": "error", "error": "actNotFound"})
    with pytest.raises(XrplReadError, match="actNotFound"):
        c.at("account_info", {"account": "rX"}, 100)


def test_validated_index_comes_from_server_state():
    c, _ = client(lambda m, p: {"status": "success", "state": {"validated_ledger": {"seq": 123}}})
    assert c.validated_index() == 123
    c2, _ = client(lambda m, p: {"status": "success", "state": {}})
    with pytest.raises(XrplReadError, match="no validated ledger"):
        c2.validated_index()


def test_http_failure_is_a_read_error():
    def boom(request):
        raise httpx.ConnectError("down")
    c = ReadOnlyClient("https://example.invalid/", transport=httpx.MockTransport(boom))
    with pytest.raises(XrplReadError):
        c.server()


# --- the websocket client (M7 step 3) ----------------------------------------------------

@pytest.mark.parametrize("method", WRITE_METHODS)
def test_websocket_refuses_write_commands_before_sending(method):
    import asyncio
    from debtlab.xrpl_market.wsclient import ReadOnlyWebsocket
    ws = ReadOnlyWebsocket("ws://127.0.0.1:9")      # never connected: refusal must come first
    with pytest.raises(NotReadOnly):
        asyncio.run(ws.command(method))


@pytest.mark.parametrize("index", ["validated", "current", None])
def test_websocket_ledger_reads_must_be_pinned(index):
    import asyncio
    from debtlab.xrpl_market.wsclient import ReadOnlyWebsocket
    ws = ReadOnlyWebsocket("ws://127.0.0.1:9")
    with pytest.raises(XrplReadError, match="validated ledger number"):
        asyncio.run(ws.command("book_offers", ledger_index=index))
