"""What the validated ledger says about the account, on testnet or mainnet. READ-ONLY: public reads only.

The ledger is the source of truth (M8): the bot's own records are checked against this on
every ledger. Every read names a validated ledger number, through `readonly.ReadOnlyClient`,
which can't send anything but a public read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from debtlab.money import D, ZERO
from debtlab.xrpl_market.live import guards
from debtlab.xrpl_market.readonly import NotReadOnly, ReadOnlyClient, Response, XrplReadError

DROPS = Decimal(1_000_000)
READ_TIMEOUT_SECONDS = 5.0
PAGE = 200
MAX_PAGES = 50


@dataclass(frozen=True)
class Header:
    index: int
    close_time: int          # Ripple-epoch seconds, rounded by the network
    resolution: int
    close_flags: int


@dataclass(frozen=True)
class LedgerOffer:
    sequence: int
    side: str                # ours: buy (we give RLUSD for XRP) or sell
    xrp: Decimal
    rlusd: Decimal
    expiration: int | None


@dataclass(frozen=True)
class AccountState:
    address: str
    ledger_index: int
    xrp: Decimal
    rlusd: Decimal
    sequence: int
    owner_count: int
    has_trust_line: bool
    offers: tuple[LedgerOffer, ...]


def parse_offer(o: dict, rlusd_hex: str, issuer: str) -> LedgerOffer | None:
    """An `account_offers` entry as one of ours. None if it isn't on the verified pair."""
    gets, pays = o.get("taker_gets"), o.get("taker_pays")

    def token(a) -> Decimal | None:
        if isinstance(a, dict) and a.get("currency") == rlusd_hex and a.get("issuer") == issuer:
            return D(a["value"])
        return None
    if isinstance(gets, str) and token(pays) is not None:
        side, xrp, rlusd = "sell", D(gets) / DROPS, token(pays)
    elif isinstance(pays, str) and token(gets) is not None:
        side, xrp, rlusd = "buy", D(pays) / DROPS, token(gets)
    else:
        return None
    exp = o.get("expiration")
    return LedgerOffer(int(o["seq"]), side, xrp, rlusd, None if exp is None else int(exp))


@dataclass(frozen=True)
class Setup:
    """How the account is set up, for the mainnet start-up checks (`live/setup.py`)."""
    address: str
    ledger_index: int
    regular_key: str | None
    lines: tuple[dict, ...]          # every trust line, as account_lines gives it
    objects: tuple[str, ...]         # the LedgerEntryType of every object the account owns
    issuer_flags: int


class FailoverReads:
    """The read client over several endpoints of one network. When one stops answering, the next is tried
    and stays in use until it fails too. Each is checked to be on the network the first time it's used, and
    every read keeps the read client's rules: a reply that isn't validated counts as a failed read."""

    def __init__(self, urls: Iterable[str], net: guards.Network = guards.TESTNET):
        self.net = net
        self.urls = [net.check_endpoint(u) for u in urls]
        if not self.urls:
            raise XrplReadError(f"no {net.name} read endpoints configured")
        self._clients: dict[str, ReadOnlyClient] = {}
        self._checked: set[str] = set()
        self.index = 0

    @property
    def url(self) -> str:
        return self.urls[self.index]

    def _client(self, url: str) -> ReadOnlyClient:
        if url not in self._clients:
            self._clients[url] = ReadOnlyClient(url, timeout=READ_TIMEOUT_SECONDS)
        return self._clients[url]

    def _try(self, call: Callable[[ReadOnlyClient], object]):
        last = None
        for step in range(len(self.urls)):
            i = (self.index + step) % len(self.urls)
            c = self._client(self.urls[i])
            try:
                if self.urls[i] not in self._checked:
                    self.net.check_network(c.server("server_info").result)    # another network raises, and doesn't fail over
                    self._checked.add(self.urls[i])
                result = call(c)
                self.index = i
                return result
            except NotReadOnly:
                raise
            except XrplReadError as e:
                last = e
        raise last

    def validated_index(self) -> int:
        return self._try(lambda c: c.validated_index())

    def server(self, method: str = "server_state") -> Response:
        return self._try(lambda c: c.server(method))

    def at(self, method: str, params: dict, ledger_index: int) -> Response:
        return self._try(lambda c: c.at(method, params, ledger_index))

    def history(self, method: str, params: dict) -> Response:
        return self._try(lambda c: c.history(method, params))


class TestnetLedger:
    __test__ = False       # not a pytest class
    net = guards.TESTNET

    def __init__(self, cfg: dict, client: FailoverReads | None = None):
        self.net.check_config(cfg)
        self.rlusd_hex, self.issuer = cfg["rlusd"]["currency_hex"], cfg["rlusd"]["issuer"]
        self.client = client or FailoverReads(cfg["live"]["endpoints"]["json_rpc"], self.net)

    def check_network(self) -> None:
        self.net.check_network(self.client.server("server_info").result)

    def latest(self) -> Header:
        i = self.client.validated_index()
        led = self.client.at("ledger", {}, i).result["ledger"]
        return Header(i, int(led["close_time"]), int(led["close_time_resolution"]), int(led.get("close_flags", 0)))

    def account(self, address: str, ledger_index: int) -> AccountState:
        data = self.client.at("account_info", {"account": address}, ledger_index).result["account_data"]
        lines = self.client.at("account_lines", {"account": address, "peer": self.issuer}, ledger_index).result.get("lines", [])
        line = next((ln for ln in lines if ln.get("currency") == self.rlusd_hex), None)
        offers = []
        marker = None
        for _ in range(MAX_PAGES):
            params = {"account": address, "limit": PAGE, **({"marker": marker} if marker else {})}
            r = self.client.at("account_offers", params, ledger_index).result
            offers += r.get("offers", [])
            marker = r.get("marker")
            if not marker:
                break
        else:
            raise XrplReadError("account_offers: too many pages")
        parsed = []
        for o in offers:
            p = parse_offer(o, self.rlusd_hex, self.issuer)
            # An offer on anything else stays visible as an unknown one, so the bot stops.
            parsed.append(p or LedgerOffer(int(o["seq"]), "other", ZERO, ZERO, o.get("expiration")))
        return AccountState(address, ledger_index, D(data["Balance"]) / DROPS, D(line["balance"]) if line else ZERO,
                            int(data["Sequence"]), int(data.get("OwnerCount", 0)), line is not None, tuple(parsed))

    def transactions(self, address: str, after_ledger: int, upto_ledger: int) -> list[dict]:
        """Every validated transaction touching the account in (after, upto], oldest first."""
        if upto_ledger <= after_ledger:
            return []
        out, marker = [], None
        for _ in range(MAX_PAGES):
            params = {"account": address, "ledger_index_min": after_ledger + 1, "ledger_index_max": upto_ledger,
                      "forward": True, "limit": PAGE, **({"marker": marker} if marker else {})}
            r = self.client.history("account_tx", params).result
            for t in r.get("transactions", []):
                if t.get("validated") is not True:
                    raise XrplReadError("account_tx returned an unvalidated transaction")
                out.append(t)
            marker = r.get("marker")
            if not marker:
                return out
        raise XrplReadError("account_tx: too many pages")


    def regular_key(self, address: str) -> str | None:
        """The account's RegularKey on the latest validated ledger, or None."""
        i = self.client.validated_index()
        return self.client.at("account_info", {"account": address}, i).result["account_data"].get("RegularKey")

    def setup(self, address: str, ledger_index: int) -> Setup:
        """Every trust line and owned object, the regular key and the issuer's flags, at one validated ledger."""
        data = self.client.at("account_info", {"account": address}, ledger_index).result["account_data"]
        lines, objects = [], []
        for method, key, out in (("account_lines", "lines", lines), ("account_objects", "account_objects", objects)):
            marker = None
            for _ in range(MAX_PAGES):
                params = {"account": address, "limit": PAGE, **({"marker": marker} if marker else {})}
                r = self.client.at(method, params, ledger_index).result
                out += r.get(key, [])
                marker = r.get("marker")
                if not marker:
                    break
            else:
                raise XrplReadError(f"{method}: too many pages")
        issuer = self.client.at("account_info", {"account": self.issuer}, ledger_index).result["account_data"]
        return Setup(address, ledger_index, data.get("RegularKey"), tuple(lines),
                     tuple(str(o.get("LedgerEntryType")) for o in objects), int(issuer.get("Flags", 0)))


class MainnetLedger(TestnetLedger):
    """The same reads, on mainnet's allowlist and network, for the mainnet bot. READ-ONLY."""
    net = guards.MAINNET


def exists_on_mainnet(address: str, urls: Iterable[str], client: Callable[[str], ReadOnlyClient] | None = None) -> bool:
    """A READ of mainnet: does this account exist there? If no endpoint answers, this raises,
    and the signer doesn't open: not knowing is a refusal."""
    last = None
    for url in urls:
        with (client or ReadOnlyClient)(url) as c:
            try:
                c.at("account_info", {"account": address}, c.validated_index())
                return True
            except XrplReadError as e:
                if "actNotFound" in str(e):
                    return False
                last = e
    raise XrplReadError(f"couldn't check mainnet for {address}: {last}")
