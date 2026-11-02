"""The only file that signs or submits (M8). TESTNET ONLY. It can only place and cancel offers.

`open_signer` checks the config, then the key file's location and who can read it, reads
the seed once, and refuses a key whose account exists on mainnet. The wallet then lives
only inside a closure: a `Signer` has no attribute holding it, won't pickle or copy, and
this file has no print, logging or environment access (tested).

`Submitter` checks its endpoint when created and the server's network before every submit.
It sends only `server_info` and a signed blob.

Every offer carries `tfPassive`, an `Expiration` and a `LastLedgerSequence`, on the verified
pair. No post-only flag exists, so `tfPassive` alone doesn't promise we never take: the
risk check keeps a price margin and the bot pauses on a fill that takes (ROADMAP, M8).
"""

from __future__ import annotations

import itertools
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


def _signing_function(wallet: Wallet) -> Callable:
    def signing(tx):
        _only_offers(tx)
        return sign(tx, wallet)
    return signing


def _whole(value: int | None, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive whole number, not {value!r}")
    return value


class Signer:
    __slots__ = ("address", "_issuer", "_currency", "_signing")

    def __init__(self, address: str, issuer: str, currency: str, signing: Callable):
        self.address, self._issuer, self._currency, self._signing = address, issuer, currency, signing

    def __repr__(self) -> str:
        return f"Signer(testnet, {self.address})"

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
    guards.check_config(cfg)
    path = guards.check_key_path(key_file, repo_root, acl=acl, me=me)
    try:
        wallet = Wallet.from_seed(path.read_text(encoding="utf-8").strip(), master_address=account)
        own = Wallet.from_seed(path.read_text(encoding="utf-8").strip()).address
    except Exception:
        raise KeyFileRefused(f"{path} doesn't hold a valid seed") from None
    for address in {wallet.address, own}:
        guards.check_not_on_mainnet(address, on_mainnet)
    return Signer(wallet.address, cfg["rlusd"]["issuer"], cfg["rlusd"]["currency_hex"], _signing_function(wallet))


class Submitter:
    """Sends signed offers to one testnet endpoint, one at a time. Connects when first needed."""

    def __init__(self, url: str, *, connect: Callable = ws_connect, timeout: float = 10.0):
        self.url = guards.check_endpoint(url)
        self._connect, self._timeout = connect, timeout
        self._conn = None
        self._ids = itertools.count(1)

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

    def submit(self, signed: Signed) -> dict:
        """The server's preliminary answer. Only a validated ledger says what really happened."""
        try:
            info = self._request("server_info")
        except Exception as e:
            self.close()
            raise SubmitFailed(f"server_info at {self.url}: {type(e).__name__}", sent=False) from None
        guards.check_network(info)
        try:
            return self._request("submit", tx_blob=signed.blob)
        except Exception as e:
            self.close()
            raise SubmitFailed(f"submit at {self.url}: {type(e).__name__}", sent=True) from None
