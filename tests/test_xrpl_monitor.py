"""The live monitor end to end, against two fake XRP Ledger servers on this machine.

Server A answers like the real thing (the saved 1 Nov responses, re-stamped with each
requested ledger), sends three validated ledgers and a real AMM swap, then drops the
connection. The monitor must record the outage, fail over to server B, carry on, and
never send anything but pinned, public reads.
"""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from debtlab.xrpl_market import fx, load_config, store
from debtlab.xrpl_market.monitor import RIPPLE_EPOCH, Monitor
from debtlab.xrpl_market.readonly import ALLOWED, LEDGER_METHODS

REAL = Path(__file__).parent / "fixtures" / "real" / "xrpl"
PROBE = REAL / "2026-11-01"
SWAP = REAL / "tx_2026-11-01" / "tx_DF01869D84B23F387C2172589E3266EE70C3669DB89FAF4DC54F58586BAF91D3.json"
pytestmark = pytest.mark.skipif(not PROBE.exists() or not SWAP.exists(), reason="real fixtures missing")


def res(name):
    return json.loads((PROBE / name).read_text(encoding="utf-8"))["result"]


class FakeLedger:
    def __init__(self, first, ledgers, drop_after, send_swap):
        self.first, self.ledgers, self.drop_after, self.send_swap = first, ledgers, drop_after, send_swap
        self.seen: list[dict] = []
        self.r = {"bids": res("book_bids_0.json"), "asks": res("book_asks_0.json"), "amm": res("amm_info.json"),
                  "issuer": res("issuer_account_info.json"), "state": res("server_state.json")}
        self.rlusd = load_config()["rlusd"]["currency_hex"]

    def answer(self, m):
        cmd, li = m["command"], m.get("ledger_index")
        if cmd == "subscribe":
            return {"ledger_index": self.first} if "streams" in m else {}
        if cmd == "book_offers":
            r = copy.deepcopy(self.r["bids" if m["taker_gets"].get("currency") == self.rlusd else "asks"])
            r["offers"] = r["offers"][: m["limit"]]
        elif cmd == "amm_info":
            r = copy.deepcopy(self.r["amm"])
        elif cmd == "account_info":
            r = copy.deepcopy(self.r["issuer"])
        elif cmd == "server_state":
            return self.r["state"]
        else:
            raise AssertionError(f"unexpected command {cmd}")
        r["ledger_index"], r["validated"] = li, True
        return r

    async def handler(self, ws):
        async def pump():
            for i in range(1, self.ledgers + 1):
                await asyncio.sleep(0.15)
                li = self.first + i
                close = datetime.now(timezone.utc) - timedelta(seconds=1)
                await ws.send(json.dumps({"type": "ledgerClosed", "ledger_index": li,
                                          "ledger_time": int(close.timestamp()) - RIPPLE_EPOCH}))
                if i == 1 and self.send_swap:
                    await ws.send(SWAP.read_text(encoding="utf-8"))
            if self.drop_after:
                await asyncio.sleep(0.3)
                await ws.close()

        task = None
        async for raw in ws:
            m = json.loads(raw)
            self.seen.append(m)
            await ws.send(json.dumps({"id": m["id"], "status": "success", "type": "response",
                                      "result": self.answer(m)}))
            if m["command"] == "subscribe" and "streams" in m and task is None:
                task = asyncio.create_task(pump())


async def scenario(conn):
    a = FakeLedger(first=1000, ledgers=3, drop_after=True, send_swap=True)
    b = FakeLedger(first=1005, ledgers=3, drop_after=False, send_swap=False)
    async with serve(a.handler, "127.0.0.1", 0) as sa, serve(b.handler, "127.0.0.1", 0) as sb:
        pa, pb = (s.sockets[0].getsockname()[1] for s in (sa, sb))
        cfg = load_config()
        cfg["endpoints"]["websocket"] = [f"ws://127.0.0.1:{pa}", f"ws://127.0.0.1:{pb}"]
        cfg["monitor"]["deep_every_ledgers"] = 2
        rate = fx.parse_ecb_eurusd(next(REAL.glob("ecb_eurusd_*.csv")).read_text(encoding="utf-8"))
        lines = []
        m = Monitor(conn, cfg, until=datetime.now(timezone.utc) + timedelta(seconds=4.5),
                    out=lines.append, fetch_fx=lambda: (rate, ""))
        await m.run()
    return a, b, lines


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    conn = store.open_db(tmp_path_factory.mktemp("monitor") / "xrpl.sqlite3")
    a, b, lines = asyncio.run(scenario(conn))
    yield conn, a, b, lines
    conn.close()


def test_snapshots_from_both_servers(ran):
    conn, *_ = ran
    rows = conn.execute("SELECT ledger_index, kind, valid, endpoint FROM xrpl_snapshots ORDER BY ledger_index").fetchall()
    assert [r["ledger_index"] for r in rows] == [1001, 1002, 1003, 1006, 1007, 1008]
    assert all(r["valid"] for r in rows)
    assert {r["ledger_index"]: r["kind"] for r in rows}[1002] == "deep"
    assert len({r["endpoint"] for r in rows}) == 2


def test_drop_recorded_and_failed_over(ran):
    conn, a, b, lines = ran
    (o,) = conn.execute("SELECT * FROM xrpl_outages").fetchall()
    assert o["kind"] == "disconnected" and o["ended_at"] is not None
    assert "2 ledger(s) not read: 1004..1005" in o["detail"]
    kinds = [e["kind"] for e in conn.execute("SELECT kind FROM xrpl_events ORDER BY id")]
    assert kinds.count("connected") == 2 and "failover" in kinds
    run = conn.execute("SELECT * FROM xrpl_runs").fetchone()
    assert run["stop_reason"] == "finished (--for)"
    assert any("failed" in line for line in lines)


def test_trade_issuer_network_fx_recorded(ran):
    conn, *_ = ran
    (t,) = conn.execute("SELECT * FROM xrpl_trades").fetchall()
    assert (t["venue"], t["taker_side"], t["xrp"]) == ("amm", "sell_xrp", "15.513009")
    assert conn.execute("SELECT COUNT(*) FROM xrpl_issuer_checks").fetchone()[0] == 2   # once per connection
    assert conn.execute("SELECT reserve_base_xrp FROM xrpl_network").fetchone()[0] == "1"
    assert store.latest_fx(conn).usd_per_eur > 0


def test_only_pinned_public_reads_were_sent(ran):
    _, a, b, _ = ran
    sent = a.seen + b.seen
    assert {m["command"] for m in sent} <= ALLOWED
    assert all(isinstance(m["ledger_index"], int) for m in sent if m["command"] in LEDGER_METHODS)
    limits = {m["limit"] for m in sent if m["command"] == "book_offers"}
    cfg = load_config()["monitor"]
    assert limits == {cfg["top_limit"], cfg["deep_limit"]}
