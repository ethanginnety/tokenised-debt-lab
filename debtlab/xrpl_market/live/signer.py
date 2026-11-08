"""The testnet signer, and the signing and sending both networks share (M8). It can only place and cancel offers.

With `live/mainnet_signer.py`, the only files that sign or submit. Opening a signer here is
TESTNET ONLY; the mainnet signer reuses `Signer` and `Submitter` with mainnet's own checks.

`open_signer` checks the config, then the key file's location and who can read it, reads
the seed once, and refuses a key whose account exists on mainnet. `open_cancel_signer` does
the same but for the mainnet check, and can sign `OfferCancel` and nothing else: cancelling
can only reduce risk, so the kill switch never depends on reaching mainnet. The wallet then lives
only inside a closure: a `Signer` has no attribute holding it, won't pickle or copy, and
this file has no print, logging or environment access (tested).

`Submitter` checks its endpoints when created and the server's network before every submit,
for the network it is given (testnet unless told otherwise).
It sends only `server_info` and a signed blob. If an endpoint doesn't answer before sending,
the next is used. A signed blob is never resent: once it may have left, its outcome is
unknown until a validated ledger says, and only the next transaction moves on.

Every offer carries `tfPassive`, an `Expiration` and a `LastLedgerSequence`, on the verified
pair. No post-only flag exists, so `tfPassive` alone doesn't promise we never take: the
risk check keeps a price margin and the bot pauses on a fill that takes (ROADMAP, M8).
"""

from __future__ import annotations

import itertools
import math
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from websockets.sync.client import connect as ws_connect
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import OfferCancel, OfferCreate, OfferCreateFlag
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from debtlab.xrpl_market.live import guards
from debtlab.xrpl_market.live.guards import KeyFileRefused

ROOT = Path(__file__).resolve().parent.parent.parent.parent
MAX_FEE_DROPS = 1000        # 100 times the base fee: anything above is a mistake, not a busy network
DROPS = Decimal(1_000_000)
IOU_DIGITS = 15


class SubmitFailed(Exception):
    """The submit didn't complete. `sent`: the transaction may have reached the server, so its outcome is unknown."""

    def __init__(self, message: str, sent: bool):
        super().__init__(message)
        self.sent = sent


@dataclass(frozen=True)
class Signed:
    blob: str
    hash: str
    kind: str               # OfferCreate | OfferCancel
    sequence: int
    last_ledger: int
    offer_sequence: int | None = None     # the offer this cancels or replaces


def _only_offers(tx) -> None:
    if not isinstance(tx, (OfferCreate, OfferCancel)):
        raise TypeError(f"only OfferCreate and OfferCancel can be signed, not {type(tx).__name__}")


def _signing_function(wallet: Wallet, only: tuple[type, ...] = (OfferCreate, OfferCancel)) -> Callable:
    def signing(tx):
        _only_offers(tx)
        if not isinstance(tx, only):
            raise TypeError(f"this signer can only cancel, not sign a {type(tx).__name__}")
        return sign(tx, wallet)
    return signing


def _whole(value: int | None, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive whole number, not {value!r}")
    return value


class Signer:
    """Signs for `address`. `key_address`: the key's own address, which differs from `address` for a regular key."""
    __slots__ = ("address", "key_address", "network", "_issuer", "_currency", "_signing")

    def __init__(self, address: str, issuer: str, currency: str, signing: Callable, network: str = "testnet",
                 key_address: str | None = None):
        self.address, self._issuer, self._currency, self._signing = address, issuer, currency, signing
        self.network, self.key_address = network, key_address or address

    def __repr__(self) -> str:
        return f"Signer({self.network}, {self.address})"

    def __reduce_ex__(self, protocol):
        raise TypeError("a Signer can't be pickled or copied")

    def _finish(self, tx, kind: str, offer_sequence: int | None) -> Signed:
        s = self._signing(tx)
        return Signed(s.blob(), s.get_hash(), kind, tx.sequence, tx.last_ledger_sequence, offer_sequence)

    def create_offer(self, *, side: str, price, size, sequence: int, expiration: int, last_ledger: int, fee_drops: int,
                     replaces: int | None = None) -> Signed:
        """A passive, self-expiring offer. `side` is ours, in XRP; `replaces` cancels that offer in the same transaction."""
        if side not in ("buy", "sell"):
            raise ValueError(f"unknown side {side!r}")
        p, q = Decimal(str(price)), Decimal(str(size))
        if p <= 0 or q <= 0:
            raise ValueError(f"malformed offer: {q} at {p}")
        drops = q * DROPS
        if drops != drops.to_integral_value():
            raise ValueError(f"size {q} XRP isn't whole drops")
        value = q * p
        if len(value.normalize().as_tuple().digits) > IOU_DIGITS:
            raise ValueError(f"RLUSD amount {value} has more than {IOU_DIGITS} significant digits")
        fee = _whole(fee_drops, "fee_drops")
        if fee > MAX_FEE_DROPS:
            raise ValueError(f"fee {fee} drops is over the maximum {MAX_FEE_DROPS}")
        xrp = str(int(drops))
        token = IssuedCurrencyAmount(currency=self._currency, issuer=self._issuer, value=format(value.normalize(), "f"))
        gets, pays = (token, xrp) if side == "buy" else (xrp, token)
        tx = OfferCreate(account=self.address, sequence=_whole(sequence, "sequence"), fee=str(fee),
                         last_ledger_sequence=_whole(last_ledger, "last_ledger"),
                         expiration=_whole(expiration, "expiration"), flags=OfferCreateFlag.TF_PASSIVE,
                         taker_gets=gets, taker_pays=pays,
                         offer_sequence=None if replaces is None else _whole(replaces, "replaces"))
        return self._finish(tx, "OfferCreate", replaces)

    def cancel_offer(self, *, offer_sequence: int, sequence: int, last_ledger: int, fee_drops: int) -> Signed:
        fee = _whole(fee_drops, "fee_drops")
        if fee > MAX_FEE_DROPS:
            raise ValueError(f"fee {fee} drops is over the maximum {MAX_FEE_DROPS}")
        tx = OfferCancel(account=self.address, sequence=_whole(sequence, "sequence"), fee=str(fee),
                         last_ledger_sequence=_whole(last_ledger, "last_ledger"),
                         offer_sequence=_whole(offer_sequence, "offer_sequence"))
        return self._finish(tx, "OfferCancel", offer_sequence)


def open_signer(key_file: Path | str, cfg: dict, *, on_mainnet: Callable[[str], bool], account: str | None = None,
                acl: Callable = guards.read_acl, me: Callable[[], str] = guards.current_user,
                repo_root: Path = ROOT) -> Signer:
    """Config first, then the file's location, then the seed. `on_mainnet` is required: there is no way to skip it.

    `account`: the account a regular key signs for; without it, the seed's own account."""
    wallet, own = _load(key_file, cfg, account, acl, me, repo_root)
    for address in {wallet.address, own}:
        guards.check_not_on_mainnet(address, on_mainnet)
    return Signer(wallet.address, cfg["rlusd"]["issuer"], cfg["rlusd"]["currency_hex"], _signing_function(wallet),
                  key_address=own)


def open_cancel_signer(key_file: Path | str, cfg: dict, *, account: str | None = None, acl: Callable = guards.read_acl,
                       me: Callable[[], str] = guards.current_user, repo_root: Path = ROOT) -> Signer:
    """For the kill switch: every check but the mainnet one, and it can sign only `OfferCancel`."""
    wallet, own = _load(key_file, cfg, account, acl, me, repo_root)
    return Signer(wallet.address, cfg["rlusd"]["issuer"], cfg["rlusd"]["currency_hex"],
                  _signing_function(wallet, (OfferCancel,)), key_address=own)


def _load(key_file: Path | str, cfg: dict, account: str | None, acl: Callable, me: Callable[[], str],
          repo_root: Path) -> tuple[Wallet, str]:
    guards.check_config(cfg)
    path = guards.check_key_path(key_file, repo_root, acl=acl, me=me)
    try:
        wallet = Wallet.from_seed(path.read_text(encoding="utf-8").strip(), master_address=account)
        own = Wallet.from_seed(path.read_text(encoding="utf-8").strip()).address
    except Exception:
        raise KeyFileRefused(f"{path} doesn't hold a valid seed") from None
    return wallet, own


class Submitter:
    """Sends signed offers to one network (testnet unless `net` says otherwise), one at a time, moving to the
    next endpoint when one stops answering."""

    def __init__(self, url: str, *more: str, connect: Callable = ws_connect, timeout: float = 10.0,
                 net: guards.Network = guards.TESTNET):
        self.net = net
        self.urls = [net.check_endpoint(u) for u in (url, *more)]
        self._index = 0
        self._connect, self._timeout = connect, timeout
        self._conn = None
        self._ids = itertools.count(1)

    @property
    def url(self) -> str:
        return self.urls[self._index]

    def _next_endpoint(self) -> None:
        self.close()
        self._index = (self._index + 1) % len(self.urls)

    def _server_info(self) -> dict | None:
        """From the first endpoint that answers, starting at the current one. None if none do."""
        for _ in self.urls:
            try:
                return self._request("server_info")
            except Exception:
                self._next_endpoint()
        return None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _request(self, command: str, **params) -> dict:
        if self._conn is None:
            self._conn = self._connect(self.url, open_timeout=self._timeout)
        rid = next(self._ids)
        self._conn.send(json.dumps({"id": rid, "command": command, **params}))
        while True:
            msg = json.loads(self._conn.recv(timeout=self._timeout))
            if msg.get("id") == rid:
                break
        if msg.get("status") != "success":
            raise RuntimeError(f"{command}: {msg.get('error')} {msg.get('error_message', '')}".strip())
        return msg.get("result", {})

    def alive(self) -> bool:
        """Is an endpoint answering, as this network? Sends only `server_info`; another network raises."""
        info = self._server_info()
        if info is None:
            return False
        self.net.check_network(info)
        return True

    def open_ledger_fee_drops(self) -> int | None:
        """What a transaction must pay now to get into the open ledger, in drops, from `server_info`: its load
        factor (the highest of the server's load and the open-ledger cost) times the base fee. None if no
        endpoint answers or it doesn't say. Another network raises."""
        info = self._server_info()
        if info is None:
            return None
        self.net.check_network(info)
        info = info.get("info") or {}
        load, base = info.get("load_factor"), (info.get("validated_ledger") or {}).get("base_fee_xrp")
        if load is None or base is None:
            return None
        return math.ceil(Decimal(str(base)) * DROPS * Decimal(str(load)))

    def submit(self, signed: Signed) -> dict:
        """The server's preliminary answer. Only a validated ledger says what really happened."""
        info = self._server_info()
        if info is None:
            raise SubmitFailed(f"no {self.net.name} endpoint answered: {', '.join(self.urls)}", sent=False)
        self.net.check_network(info)
        try:
            return self._request("submit", tx_blob=signed.blob)
        except Exception as e:
            where = self.url
            self._next_endpoint()
            raise SubmitFailed(f"submit at {where}: {type(e).__name__}", sent=True) from None
