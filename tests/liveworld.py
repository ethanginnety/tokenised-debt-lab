"""A fake XRP Ledger testnet for the live venue's tests. Nothing touches a network.

It behaves as far as the docs and the saved real transactions show: it decodes the real
signed blobs from the real signer, applies them when a ledger closes, checks Sequence,
LastLedgerSequence and Expiration against the PREVIOUS close, removes `OfferSequence` first,
lets a crossing offer take liquidity, and writes metadata in rippled's layout. Close times
bunch after 10 s boundaries, like the real ones. The key is a fake from all-zero entropy.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from datetime import date, datetime, timedelta, timezone

import httpx
from xrpl.core import keypairs
from xrpl.core.binarycodec import decode
from xrpl.wallet import Wallet

from debtlab.money import plain
from debtlab.xrpl_market import book as xbook
from debtlab.xrpl_market import load_testnet_config, store
from debtlab.xrpl_market.fx import FxRate
from debtlab.xrpl_market.verify import Amm, NetworkCosts
from debtlab.xrpl_market.readonly import XrplReadError

D = Decimal
CFG = load_testnet_config()
ISSUER, RLUSD = CFG["rlusd"]["issuer"], CFG["rlusd"]["currency_hex"]
SEED = keypairs.generate_seed(entropy="00" * 16)
ADDRESS = Wallet.from_seed(SEED).address
DROPS = 1_000_000
START_CLOSE = 800_000_000


def rl(v) -> dict:
    return {"currency": RLUSD, "issuer": ISSUER, "value": plain(D(v))}


def write_key(tmp_path: Path) -> Path:
    p = tmp_path / "xrpl-testnet.key"
    p.write_text(SEED + "\n", encoding="utf-8")
    return p


def resp(result: dict):
    return SimpleNamespace(result={**result, "status": "success"})


class FakeNet:
    def __init__(self, xrp="300", rlusd="20", address=ADDRESS):
        self.address = address
        self.xrp, self.rlusd, self.seq = int(D(xrp) * DROPS), D(rlusd), 100
        self.offers: dict[int, dict] = {}
        self.index, self.close_time = 1000, START_CLOSE
        self.parent_close = START_CLOSE - 1
        self.resolution, self.close_flags, self.network_id = 10, 0, 1
        self.queue: list[dict] = []
        self.history: list[dict] = []
        self.sent: list[dict] = []
        self.urls: list[str] = []
        self.hits: list[tuple] = []
        self.payments: list[tuple] = []
        self.best_bid, self.best_ask = D("0.0795"), D("0.0805")
        self.fail_connect = False
        self.drop_all = False            # accepted, then never reaches a ledger
        self.no_answer_to_submit = False
        self.force_prelim: str | None = None
        self.reads_fail = False
        self.ntx = 0
        self._out: list[str] = []
        self.has_line = True
        self.fill_rounding = D(0)     # RLUSD: a balance moving by a hair more than the offer did
        self.down_urls: set[str] = set()

    # --- the signer's connection ---------------------------------------------------------

    def __call__(self, url, **_):
        if self.fail_connect or url in self.down_urls:
            raise ConnectionError("down")
        self.urls.append(url)
        return self

    def send(self, raw):
        if self.fail_connect:
            raise ConnectionError("down")
        m = json.loads(raw)
        self.sent.append(m)
        if m["command"] == "server_info":
            self._out.append(json.dumps({"id": m["id"], "status": "success", "result": {"info": {"network_id": self.network_id}}}))
            return
        assert m["command"] == "submit"
        if self.no_answer_to_submit:
            tx = decode(m["tx_blob"])
            self.queue.append(tx)
            raise TimeoutError("no answer")
        tx = decode(m["tx_blob"])
        code = self.force_prelim or self._precheck(tx)
        if code in ("tesSUCCESS",) and not self.drop_all:
            self.queue.append(tx)
        self._out.append(json.dumps({"id": m["id"], "status": "success", "result": {"engine_result": code}}))

    def recv(self, timeout=None):
        return self._out.pop(0)

    def close_conn(self):
        pass

    def close(self, *a, **k):
        """Closes the connection when called by the Submitter; `close_ledger` is the ledger."""

    def blobs(self):
        return [decode(m["tx_blob"]) for m in self.sent if m["command"] == "submit"]

    def _precheck(self, tx) -> str:
        expected = self.seq + len(self.queue)
        if tx["Sequence"] < expected:
            return "tefPAST_SEQ"
        if tx["Sequence"] > expected:
            return "terPRE_SEQ"
        return "tesSUCCESS"

    # --- the read client ---------------------------------------------------------------

    def validated_index(self) -> int:
        if self.reads_fail:
            raise XrplReadError("server_state: down")
        return self.index

    def server(self, method="server_state"):
        if self.reads_fail:
            raise XrplReadError(f"{method}: down")
        return resp({"info": {"network_id": self.network_id}})

    def at(self, method, params, ledger_index):
        if self.reads_fail:
            raise XrplReadError(f"{method}: down")
        if method == "ledger":
            return resp({"ledger": {"close_time": self.close_time, "close_time_resolution": self.resolution,
                                    "close_flags": self.close_flags}})
        if method == "account_info":
            return resp({"account_data": {"Balance": str(self.xrp), "Sequence": self.seq, "OwnerCount": len(self.offers) + 1}})
        if method == "account_lines":
            return resp({"lines": [{"currency": RLUSD, "account": ISSUER, "balance": plain(self.rlusd)}] if self.has_line else []})
        if method == "account_offers":
            return resp({"offers": [{"seq": s, "taker_gets": o["gets"], "taker_pays": o["pays"],
                                     **({"expiration": o["expiration"]} if o.get("expiration") else {})}
                                    for s, o in sorted(self.offers.items())]})
        raise AssertionError(method)

    def history_read(self, params):
        if self.reads_fail:
            raise XrplReadError("account_tx: down")
        return [t for t in self.history if params["ledger_index_min"] <= t["ledger_index"] <= params["ledger_index_max"]]

    # --- closing ledgers -----------------------------------------------------------------

    def _next_close(self) -> int:
        """Close times bunch after each 10 s boundary, as measured: :00, :01, :02, then :10."""
        c = self.close_time
        return c + 1 if c % 10 < 2 else (c // 10 + 1) * 10

    def _nodes(self, x0, r0, removed=(), created=None, consumed=()):
        nodes = []
        if self.xrp != x0:
            nodes.append({"ModifiedNode": {"LedgerEntryType": "AccountRoot", "LedgerIndex": "A",
                                           "FinalFields": {"Account": self.address, "Balance": str(self.xrp)},
                                           "PreviousFields": {"Balance": str(x0)}}})
        if self.rlusd != r0:
            f = {"Balance": {"currency": RLUSD, "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji", "value": plain(self.rlusd)},
                 "LowLimit": {"issuer": self.address}, "HighLimit": {"issuer": ISSUER}}
            nodes.append({"ModifiedNode": {"LedgerEntryType": "RippleState", "LedgerIndex": "L", "FinalFields": f,
                                           "PreviousFields": {"Balance": {**f["Balance"], "value": plain(r0)}}}})
        for seq, o in removed:
            nodes.append({"DeletedNode": {"LedgerEntryType": "Offer", "LedgerIndex": f"O{seq}", "FinalFields": {
                "Account": self.address, "Sequence": seq, "TakerGets": o["gets"], "TakerPays": o["pays"]}}})
        for seq, before, after, deleted in consumed:
            nodes.append({("DeletedNode" if deleted else "ModifiedNode"): {
                "LedgerEntryType": "Offer", "LedgerIndex": f"O{seq}",
                "FinalFields": {"Account": self.address, "Sequence": seq, "TakerGets": after["gets"], "TakerPays": after["pays"]},
                "PreviousFields": {"TakerGets": before["gets"], "TakerPays": before["pays"]}}})
        if created is not None:
            o = self.offers[created]
            nodes.append({"CreatedNode": {"LedgerEntryType": "Offer", "LedgerIndex": f"O{created}", "NewFields": {
                "Account": self.address, "Sequence": created, "TakerGets": o["gets"], "TakerPays": o["pays"]}}})
        return nodes

    def _record(self, tx, result, nodes):
        self.ntx += 1
        self.history.append({"validated": True, "ledger_index": self.index, "hash": tx.get("hash") or self._hash(tx),
                             "tx": tx, "meta": {"TransactionResult": result, "AffectedNodes": nodes}})

    def _hash(self, tx):
        from xrpl.core.binarycodec import encode
        from xrpl.core.keypairs.helpers import sha512_first_half
        return sha512_first_half(bytes.fromhex("54584E00" + encode(tx))).hex().upper()

    def _apply(self, tx):
        seq = tx["Sequence"]
        if seq != self.seq or tx.get("LastLedgerSequence", 10**12) < self.index:
            return
        x0, r0 = self.xrp, self.rlusd
        self.xrp -= int(tx["Fee"])
        self.seq += 1
        removed = []
        if "OfferSequence" in tx and tx["OfferSequence"] in self.offers:
            removed.append((tx["OfferSequence"], self.offers.pop(tx["OfferSequence"])))
        if tx["TransactionType"] == "OfferCancel":
            return self._record(tx, "tesSUCCESS", self._nodes(x0, r0, removed))
        if tx.get("Expiration", 10**12) <= self.parent_close:
            return self._record(tx, "tecEXPIRED", self._nodes(x0, r0, removed))
        gets, pays = tx["TakerGets"], tx["TakerPays"]
        buying = isinstance(pays, str)
        xrp = D(pays if buying else gets) / DROPS
        token = D((gets if buying else pays)["value"])
        price = token / xrp
        if (buying and price > self.best_ask) or (not buying and price < self.best_bid):
            # Crosses the book: it takes liquidity at the book's price, all of it.
            at = self.best_ask if buying else self.best_bid
            if buying:
                self.xrp += int(xrp * DROPS)
                self.rlusd -= xrp * at
            else:
                self.xrp -= int(xrp * DROPS)
                self.rlusd += xrp * at
            return self._record(tx, "tesSUCCESS", self._nodes(x0, r0, removed))
        self.offers[seq] = {"gets": gets, "pays": pays, "expiration": tx.get("Expiration")}
        return self._record(tx, "tesSUCCESS", self._nodes(x0, r0, removed, created=seq))

    def _apply_hit(self, seq, xrp):
        o = self.offers.get(seq)
        if o is None or (o.get("expiration") or 10**12) <= self.parent_close:
            return False
        x0, r0 = self.xrp, self.rlusd
        buying = isinstance(o["pays"], str)
        total_x = D(o["pays"] if buying else o["gets"]) / DROPS
        total_r = D((o["gets"] if buying else o["pays"])["value"])
        q = min(D(xrp), total_x)
        r = total_r * q / total_x
        if buying:
            self.xrp += int(q * DROPS)
            self.rlusd -= r
            after = {"gets": rl(total_r - r), "pays": str(int((total_x - q) * DROPS))}
        else:
            self.xrp -= int(q * DROPS)
            self.rlusd += r
            after = {"gets": str(int((total_x - q) * DROPS)), "pays": rl(total_r - r)}
        self.rlusd += self.fill_rounding
        done = q >= total_x
        if done:
            before = self.offers.pop(seq)
        else:
            before = dict(o)
            self.offers[seq] = {**o, **after}
        self._record({"Account": "rTaker", "TransactionType": "Payment", "hash": f"HIT{self.ntx:060d}", "Fee": "10"},
                     "tesSUCCESS", self._nodes(x0, r0, consumed=[(seq, before, after, done)]))
        return True

    def hit(self, seq, xrp):
        """Someone else's transaction consumes our offer in the next ledger."""
        self.hits.append((seq, xrp))

    def payment(self, xrp="0", rlusd="0"):
        """Someone sends the account funds from outside, in the next ledger."""
        self.payments.append((xrp, rlusd))

    def _apply_payment(self, xrp, rlusd):
        x0, r0 = self.xrp, self.rlusd
        self.xrp += int(D(xrp) * DROPS)
        self.rlusd += D(rlusd)
        self._record({"Account": "rFaucet", "TransactionType": "Payment", "hash": f"PAY{self.ntx:060d}", "Fee": "10"},
                     "tesSUCCESS", self._nodes(x0, r0))

    def close_ledger(self, n=1):
        for _ in range(n):
            self.index += 1
            self.parent_close, self.close_time = self.close_time, self._next_close()
            for seq, xrp in self.hits:
                self._apply_hit(seq, xrp)
            self.hits = []
            for xrp, rlusd in self.payments:
                self._apply_payment(xrp, rlusd)
            self.payments = []
            for tx in self.queue:
                self._apply(tx)
            self.queue = []
        return self.index


def client_for(net: FakeNet):
    """A read client backed by the fake: `history` as the real client's."""
    c = SimpleNamespace(validated_index=net.validated_index, server=net.server, at=net.at)
    c.history = lambda method, params: resp({"transactions": net.history_read(params)})
    return c


T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class Monitor:
    """A real testnet monitor database, written through `store`, for the bot to read."""

    def __init__(self, tmp_path, network="testnet", t0=T0, fx_date=date(2026, 10, 30), exclude=ADDRESS):
        self.path = tmp_path / "mon.sqlite3"
        self.exclude = exclude
        self.conn = store.open_db(self.path)
        store.claim_network(self.conn, network)
        store.record_fx(self.conn, FxRate(fx_date, D("1.12"), "ECB"), t0)
        self.run = store.start_run(self.conn, t0)
        store.record_network(self.conn, NetworkCosts(1000, D(1), D("0.2"), D(10), D(10)), t0)

    def snapshot(self, ledger, t, bid="0.0795", ask="0.0805", amm_price="0.08"):
        bid, ask, size = D(bid), D(ask), 100
        bids = {"ledger_index": ledger, "validated": True, "offers": [{"TakerGets": rl(bid * size), "TakerPays": str(size * DROPS)}]}
        asks = {"ledger_index": ledger, "validated": True, "offers": [{"TakerGets": str(size * DROPS), "TakerPays": rl(ask * size)}]}
        pool = None if amm_price is None else Amm(ledger, "rAmm", D(1000), D(1000) * D(amm_price), D(amm_price), D("0.5"))
        store.record_snapshot(self.conn, self.run, "wss://testnet.xrpl-labs.com/", ledger, t - timedelta(seconds=1), t, "top",
                              xbook.parse_book([bids], [asks], RLUSD), pool, self.exclude)


TESTNET_HOSTS = {"testnet.xrpl-labs.com", "clio.altnet.rippletest.net"}


def rpc_transport(net: FakeNet, *, mainnet="absent", on_read=None, down_hosts=None):
    """The real read client over fake HTTP: the fake testnet answers in rippled's JSON-RPC layout, on
    both endpoints. Every other host counts as mainnet: `absent` says the account isn't there, `down`
    doesn't answer. `down_hosts`: a set, changeable while running, of testnet hosts that don't answer."""
    net.mainnet_reads, net.read_hosts = [], []
    down_hosts = set() if down_hosts is None else down_hosts

    def handler(request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"][0]
        if request.url.host in down_hosts:
            raise httpx.ConnectError("unreachable")
        if request.url.host in TESTNET_HOSTS:
            net.read_hosts.append(request.url.host)
        else:
            net.mainnet_reads.append(request.url.host)
            if mainnet == "down":
                raise httpx.ConnectError("unreachable")
            if method == "server_state":
                return httpx.Response(200, json={"result": {"status": "success", "state": {"validated_ledger": {"seq": 108000000}}}})
            return httpx.Response(200, json={"result": {"status": "error", "error": "actNotFound"}})
        if method == "server_state":
            if on_read:
                on_read()
            result = {"state": {"validated_ledger": {"seq": net.index}}}
        elif method == "server_info":
            result = {"info": {"network_id": net.network_id}}
        elif method == "account_tx":      # API v1's layout: hash and ledger inside `tx`
            result = {"transactions": [{"meta": t["meta"], "validated": True,
                                        "tx": {**t["tx"], "hash": t["hash"], "ledger_index": t["ledger_index"]}}
                                       for t in net.history_read(params)]}
        else:
            result = {**net.at(method, params, params["ledger_index"]).result, "validated": True,
                      "ledger_index": params["ledger_index"]}
        return httpx.Response(200, json={"result": {"status": "success", **result}})
    return httpx.MockTransport(handler)
