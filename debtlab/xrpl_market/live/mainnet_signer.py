"""The mainnet signer (M8). REAL FUNDS. Approved by Ethan on 7 Nov 2026: offers and cancels only, regular key only.

With `live/signer.py`, the only files that sign or hold a key (tests/test_xrpl_no_signing.py).
It reuses that file's `Signer`, which can build only `OfferCreate` and `OfferCancel`, and its
`Submitter`, given mainnet's checks. `open_mainnet_signer` checks, in order:

    the config is for mainnet: network 0, the verified RLUSD issuer, every endpoint allowlisted
    the key file is `xrpl-bot.key`, outside the repository and OneDrive, readable only by Ethan
    the key is not the account's master key, and IS its regular key on the validated ledger

so the master key never has to be on the laptop, and Ethan can revoke the bot's key with it.
`open_mainnet_cancel_signer` is for the kill switch: the same, but for the ledger read of the
regular key, so cancelling never depends on one more read. It can sign `OfferCancel` only.

`generate_bot_key` makes the bot's key pair straight into its file and returns only the
address: the seed is never returned, printed or logged. `ShadowSubmitter` sends nothing but
`server_info`: the shadow run reads the real account and quotes nothing.

This file has no print, logging, environment or subprocess access (tested).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from xrpl.core import keypairs
from xrpl.models.transactions import OfferCancel
from xrpl.wallet import Wallet

from debtlab.xrpl_market.live import guards
from debtlab.xrpl_market.live.guards import KeyFileRefused, MainnetBlocked
from debtlab.xrpl_market.live.signer import ROOT, Signed, Signer, SubmitFailed, Submitter, _signing_function

__all__ = ["open_mainnet_signer", "open_mainnet_cancel_signer", "generate_bot_key", "ShadowSubmitter",
           "MainnetBlocked"]


def _load(key_file: Path | str, cfg: dict, account: str, acl: Callable, me: Callable[[], str],
          repo_root: Path) -> tuple[Wallet, str]:
    guards.check_mainnet_config(cfg)
    if not account or account != cfg["live"].get("account"):
        raise MainnetBlocked(f"account {account!r} is not the account set in the mainnet config")
    path = guards.check_mainnet_key_path(key_file, repo_root, acl=acl, me=me)
    try:
        seed = path.read_text(encoding="utf-8").strip()
        wallet = Wallet.from_seed(seed, master_address=account)
        own = Wallet.from_seed(seed).address
    except Exception:
        raise KeyFileRefused(f"{path} doesn't hold a valid seed") from None
    return wallet, own


def _signer(wallet: Wallet, own: str, cfg: dict, only: tuple[type, ...] | None = None) -> Signer:
    signing = _signing_function(wallet) if only is None else _signing_function(wallet, only)
    return Signer(wallet.address, cfg["rlusd"]["issuer"], cfg["rlusd"]["currency_hex"], signing, network="mainnet",
                  key_address=own)


def open_mainnet_signer(key_file: Path | str, cfg: dict, *, account: str, regular_key: Callable[[str], str | None],
                        acl: Callable = guards.read_acl, me: Callable[[], str] = guards.current_user,
                        repo_root: Path = ROOT) -> Signer:
    """`regular_key(account)`: the account's RegularKey on the validated ledger, or None. Required: if it can't be
    read, it raises, and no signer opens."""
    wallet, own = _load(key_file, cfg, account, acl, me, repo_root)
    guards.check_regular_key(account, own, regular_key(account))
    return _signer(wallet, own, cfg)


def open_mainnet_cancel_signer(key_file: Path | str, cfg: dict, *, account: str, acl: Callable = guards.read_acl,
                               me: Callable[[], str] = guards.current_user, repo_root: Path = ROOT) -> Signer:
    """For the kill switch: never the master key, and it can sign only `OfferCancel`."""
    wallet, own = _load(key_file, cfg, account, acl, me, repo_root)
    if own == account:
        raise MainnetBlocked(f"this is {account}'s master key: on mainnet the bot signs only with a regular key")
    return _signer(wallet, own, cfg, (OfferCancel,))


def generate_bot_key(path: Path | str, *, repo_root: Path = ROOT) -> str:
    """Write a new key pair's seed to `path`, which must not exist yet, and return only its address.

    The place is checked first (name, repository, OneDrive). The file is created exclusively, so a key is
    never overwritten. Who can read it is the caller's to lock and check: this file can't run icacls."""
    guards.check_mainnet_key_name(path)
    p = guards.check_key_location(path, repo_root)
    if p.exists():
        raise KeyFileRefused(f"{p} already exists; a key is never overwritten. Remove the regular key in Xaman "
                             "and delete the file yourself first if you mean to replace it")
    p.parent.mkdir(parents=True, exist_ok=True)
    seed = keypairs.generate_seed()          # 16 bytes from `secrets`
    with p.open("x", encoding="utf-8") as f:
        f.write(seed + "\n")
    return Wallet.from_seed(seed).address


class ShadowSubmitter(Submitter):
    """Mainnet's checks, and `server_info` only: every submit is refused before anything is sent."""

    def __init__(self, url: str, *more: str, **kw):
        super().__init__(url, *more, net=guards.MAINNET, **kw)
        self.refused = 0

    def submit(self, signed: Signed) -> dict:
        self.refused += 1
        raise SubmitFailed("shadow run: nothing is sent", sent=False)
