"""A read-only websocket connection to the XRP Ledger (M7 step 3).

Same rules as `readonly.ReadOnlyClient`: only the public read commands in
`readonly.ALLOWED` can be sent (checked before anything leaves the machine), and ledger
reads must name one validated ledger and come back validated, from that ledger.
Responses are matched to requests by id; everything else (ledger closes, transactions)
goes to the stream queue.
"""

from __future__ import annotations

import asyncio
import itertools
import json

import websockets

from debtlab.xrpl_market.readonly import (ALLOWED, LEDGER_METHODS, NotReadOnly, XrplReadError)


class ReadOnlyWebsocket:
    def __init__(self, url: str, *, request_timeout: float = 10.0, open_timeout: float = 15.0):
        self.url = url
        self.request_timeout = request_timeout
        self.open_timeout = open_timeout
        self._ws = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self.stream: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "ReadOnlyWebsocket":
        self._ws = await websockets.connect(self.url, open_timeout=self.open_timeout, ping_interval=20,
                                            ping_timeout=20, max_size=16 * 2**20)
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                fut = self._pending.pop(msg.get("id"), None) if "id" in msg else None
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                else:
                    await self.stream.put(msg)
            # A clean close ends the loop without an exception: say so, or the monitor
            # would wait on a dead connection.
            await self.stream.put({"type": "_closed", "reason": "server closed the connection"})
        except websockets.ConnectionClosed as e:
            await self.stream.put({"type": "_closed", "reason": str(e)})
        except Exception as e:  # anything else ends the connection, visibly
            await self.stream.put({"type": "_closed", "reason": f"{type(e).__name__}: {e}"})
        finally:
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(XrplReadError("connection closed"))
            self._pending.clear()

    async def command(self, command: str, **params) -> dict:
        """Send one allowed command and return its result."""
        if command not in ALLOWED:
            raise NotReadOnly(f"{command!r} is not a public read command; refused")
        if command in LEDGER_METHODS:
            li = params.get("ledger_index")
            if not isinstance(li, int) or isinstance(li, bool):
                raise XrplReadError("ledger reads must name a validated ledger number")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send(json.dumps({"id": rid, "command": command, **params}))
        try:
            msg = await asyncio.wait_for(fut, self.request_timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise XrplReadError(f"{command}: no answer in {self.request_timeout}s") from e
        if msg.get("status") != "success":
            raise XrplReadError(f"{command}: {msg.get('error')} {msg.get('error_message', '')}".strip())
        result = msg.get("result", {})
        if command in LEDGER_METHODS:
            if result.get("validated") is not True:
                raise XrplReadError(f"{command}: ledger {params['ledger_index']} not validated")
            if result.get("ledger_index") is not None and int(result["ledger_index"]) != params["ledger_index"]:
                raise XrplReadError(f"{command}: asked for {params['ledger_index']}, got {result['ledger_index']}")
        return result
