"""A JSON-RPC client for the XRP Ledger that can only read.

Only the public read methods in `ALLOWED` can be sent; anything else (submit, sign,
wallet_propose, ...) is refused before a byte leaves the machine. Ledger data is
always read at an explicit, validated ledger index, and a response that isn't
validated, or comes from a different ledger, is rejected. So every snapshot is
consistent: all of its pieces come from the same validated ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

# Methods that read ledger state at a ledger index.
LEDGER_METHODS = frozenset({
    "ledger", "book_offers", "amm_info", "account_info", "account_lines", "account_offers", "gateway_balances",
    "account_objects",      # M8 mainnet: the start-up check that the account owns nothing unexpected
})
# Methods reading an account's validated history (M8); each entry says whether it's validated.
HISTORY_METHODS = frozenset({"account_tx", "tx"})
# Methods about the server, not a ledger.
SERVER_METHODS = frozenset({"server_state", "server_info"})
# Websocket only (M7 step 3); refused over JSON-RPC.
STREAM_METHODS = frozenset({"subscribe", "unsubscribe"})
ALLOWED = LEDGER_METHODS | HISTORY_METHODS | SERVER_METHODS | STREAM_METHODS


class XrplReadError(Exception):
    """A read failed or returned something we won't trust."""


class NotReadOnly(XrplReadError):
    """Someone tried to send a method that isn't a public read."""


@dataclass(frozen=True)
class Response:
    method: str
    result: dict
    raw: bytes  # exactly what the server sent, for fixtures


class ReadOnlyClient:
    def __init__(self, url: str, *, timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self.url = url
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Content-Type": "application/json"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ReadOnlyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _post(self, method: str, params: dict) -> Response:
        if method not in ALLOWED:
            raise NotReadOnly(f"{method!r} is not a public read method; refused")
        if method in STREAM_METHODS:
            raise NotReadOnly(f"{method!r} needs a websocket; not sent over JSON-RPC")
        try:
            r = self._http.post(self.url, content=json.dumps({"method": method, "params": [params]}))
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise XrplReadError(f"{method} at {self.url}: {e}") from e
        result = body.get("result")
        if not isinstance(result, dict):
            raise XrplReadError(f"{method}: no result in the response")
        if result.get("status") != "success":
            raise XrplReadError(f"{method}: {result.get('error')} {result.get('error_message', '')}".strip())
        return Response(method, result, r.content)

    def server(self, method: str = "server_state") -> Response:
        if method not in SERVER_METHODS:
            raise NotReadOnly(f"{method!r} is not a server method")
        return self._post(method, {})

    def history(self, method: str, params: dict) -> Response:
        if method not in HISTORY_METHODS:
            raise NotReadOnly(f"{method!r} is not a history method")
        return self._post(method, params)

    def validated_index(self) -> int:
        """The latest validated ledger this server has."""
        state = self.server("server_state").result["state"]
        vl = state.get("validated_ledger")
        if not vl or "seq" not in vl:
            raise XrplReadError("server has no validated ledger")
        return int(vl["seq"])

    def at(self, method: str, params: dict, ledger_index: int) -> Response:
        """Read `method` at one validated ledger. Refuses anything not validated or from another ledger."""
        if method not in LEDGER_METHODS:
            raise NotReadOnly(f"{method!r} is not a ledger read method")
        if not isinstance(ledger_index, int) or isinstance(ledger_index, bool):
            raise XrplReadError("ledger_index must be a validated ledger number, not a shortcut like 'current'")
        resp = self._post(method, {**params, "ledger_index": ledger_index})
        if resp.result.get("validated") is not True:
            raise XrplReadError(f"{method}: ledger {ledger_index} not validated")
        got = resp.result.get("ledger_index")
        if got is not None and int(got) != ledger_index:
            raise XrplReadError(f"{method}: asked for ledger {ledger_index}, got {got}")
        return resp
