"""What keeps each network's venue on its own network, and the keys safe (M8). No signing here.

Every check raises rather than returns False, so a forgotten `if` can't let anything past.
The allowlists are constants on purpose, so no config can widen them. The testnet checks refuse
mainnet (tests/test_xrpl_testnet_safety.py). The mainnet checks, approved by Ethan on 7 Nov 2026
with the mainnet plan, are separate functions with their own allowlist, sharing no host with
testnet's, and refuse everything else (tests/test_xrpl_mainnet_safety.py). On mainnet the bot
signs only with the account's regular key, never the master key, from `xrpl-bot.key` only.

A signed XRP Ledger transaction names no network (below ID 1025 the NetworkID field is
disallowed), so a testnet transaction could replay on mainnet if the same key held an
account there. Hence `check_not_on_mainnet`, as well as the endpoint and network checks.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TESTNET_NETWORK_ID = 1
TESTNET_HOSTS = frozenset({"testnet.xrpl-labs.com", "s.altnet.rippletest.net", "clio.altnet.rippletest.net"})
TESTNET_ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"
ENDPOINT_SCHEMES = frozenset({"wss", "https"})
ENDPOINT_PORTS = frozenset({None, 443, 51233, 51234})
MAINNET_KEY_NAMES = frozenset({"xrpl-bot.key"})     # the RUNBOOK's name for the mainnet bot key
MAINNET_NETWORK_ID = 0
MAINNET_HOSTS = frozenset({"xrplcluster.com", "s2.ripple.com"})     # s1 left out: it lagged its own reports (7 Nov)
MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"     # verified 1 Nov 2026 (XRPL_VERIFICATION.md)
SAFE_PRINCIPALS = frozenset({"NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"})


class MainnetBlocked(Exception):
    """Something would have reached, or could be replayed on, a network other than testnet."""


class KeyFileRefused(Exception):
    """The key file is somewhere it shouldn't be, or can be read by others. Names the path, never the contents."""


def _check_host(url: str, hosts: frozenset[str], network: str) -> str:
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError as e:
        raise MainnetBlocked(f"endpoint {url!r} can't be read: {e}") from None
    if u.scheme not in ENDPOINT_SCHEMES or (u.hostname or "").lower() not in hosts or port not in ENDPOINT_PORTS:
        raise MainnetBlocked(f"{url!r} is not an allowed {network} endpoint; only {sorted(hosts)}")
    if u.username or u.password:
        raise MainnetBlocked(f"{url!r}: credentials in an endpoint are refused")
    return url


def check_endpoint(url: str) -> str:
    """Only an allowlisted testnet host, over TLS. Everything else, mainnet included, is refused."""
    return _check_host(url, TESTNET_HOSTS, "testnet")


def check_network(server_info: dict) -> None:
    """The server itself must say it's on testnet. A missing answer is a refusal."""
    nid = (server_info.get("info") or {}).get("network_id")
    if nid != TESTNET_NETWORK_ID:
        raise MainnetBlocked(f"the server reports network {nid!r}, not testnet ({TESTNET_NETWORK_ID})")


def check_issuer(issuer: str) -> str:
    if issuer != TESTNET_ISSUER:
        raise MainnetBlocked(f"issuer {issuer!r} is not the testnet RLUSD issuer {TESTNET_ISSUER}")
    return issuer


def check_config(cfg: dict) -> None:
    """A config must say testnet, and every endpoint and the issuer must pass. Before any key is touched."""
    if cfg.get("network") != "testnet" or cfg.get("network_id") != TESTNET_NETWORK_ID:
        raise MainnetBlocked(f"config is for {cfg.get('network')!r}, not testnet")
    check_issuer(cfg["rlusd"]["issuer"])
    for group in (cfg["endpoints"], cfg.get("live", {}).get("endpoints", {})):
        for urls in group.values():
            for url in urls:
                check_endpoint(url)


def check_mainnet_endpoint(url: str) -> str:
    """Only an allowlisted mainnet host, over TLS. Testnet, and everything else, is refused."""
    return _check_host(url, MAINNET_HOSTS, "mainnet")


def check_mainnet_network(server_info: dict) -> None:
    """The server itself must say it's on mainnet (network 0). A missing answer is a refusal."""
    nid = (server_info.get("info") or {}).get("network_id")
    if nid != MAINNET_NETWORK_ID or isinstance(nid, bool):
        raise MainnetBlocked(f"the server reports network {nid!r}, not mainnet ({MAINNET_NETWORK_ID})")


def check_mainnet_issuer(issuer: str) -> str:
    if issuer != MAINNET_ISSUER:
        raise MainnetBlocked(f"issuer {issuer!r} is not the verified RLUSD issuer {MAINNET_ISSUER}")
    return issuer


def check_mainnet_config(cfg: dict) -> None:
    """A config must say mainnet, and every endpoint and the issuer must pass. Before any key is touched."""
    if cfg.get("network") != "mainnet" or cfg.get("network_id") != MAINNET_NETWORK_ID:
        raise MainnetBlocked(f"config is for {cfg.get('network')!r}, not mainnet")
    check_mainnet_issuer(cfg["rlusd"]["issuer"])
    for group in (cfg["endpoints"], cfg.get("live", {}).get("endpoints", {})):
        for urls in group.values():
            for url in urls:
                check_mainnet_endpoint(url)


def check_regular_key(account: str, key_address: str, on_ledger: str | None) -> None:
    """The bot's key must be the account's regular key on the validated ledger, and never its master key."""
    if not account:
        raise MainnetBlocked("no account is set: the bot signs only for the account in the config")
    if key_address == account:
        raise MainnetBlocked(f"this is {account}'s master key: on mainnet the bot signs only with a regular key")
    if on_ledger is None:
        raise MainnetBlocked(f"{account} has no regular key on the ledger: set it in Xaman first (RUNBOOK)")
    if on_ledger != key_address:
        raise MainnetBlocked(f"this key ({key_address}) is not {account}'s regular key on the ledger ({on_ledger})")


@dataclass(frozen=True)
class Network:
    """One network's checks. Each looks its guard up when called, so the controls in the tests can take one away."""
    name: str
    label: str
    _endpoint: str
    _network: str
    _config: str

    def check_endpoint(self, url: str) -> str:
        return globals()[self._endpoint](url)

    def check_network(self, server_info: dict) -> None:
        globals()[self._network](server_info)

    def check_config(self, cfg: dict) -> None:
        globals()[self._config](cfg)


TESTNET = Network("testnet", "TESTNET: no real funds", "check_endpoint", "check_network", "check_config")
MAINNET = Network("mainnet", "MAINNET: real funds", "check_mainnet_endpoint", "check_mainnet_network",
                  "check_mainnet_config")
NETWORKS = {n.name: n for n in (TESTNET, MAINNET)}


def network_for(cfg: dict) -> Network:
    """The network a config is for, once its own checks pass."""
    net = NETWORKS.get(cfg.get("network"))
    if net is None:
        raise MainnetBlocked(f"config is for an unknown network {cfg.get('network')!r}")
    net.check_config(cfg)
    return net


def check_not_on_mainnet(address: str, exists_on_mainnet: Callable[[str], bool]) -> None:
    """Refuse a key whose account exists on mainnet. If that can't be checked, the lookup raises: also a refusal."""
    if exists_on_mainnet(address):
        raise MainnetBlocked(f"account {address} exists on mainnet: a testnet key must never be a mainnet key")


def read_acl(path: Path) -> list[str]:
    """Who can read this file: Windows principals, or on other systems 'group' or 'other' if they can."""
    if sys.platform != "win32":
        mode = path.stat().st_mode
        return [who for who, bit in (("group", 0o040), ("other", 0o004)) if mode & bit]
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        m = re.search(r"([^\s:][^:]*):\(", line.replace(str(path), ""))
        if m:
            found.append(m.group(1).strip())
    return found


def current_user() -> str:
    if sys.platform != "win32":
        return ""
    return subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()


def check_key_location(path: Path | str, repo_root: Path) -> Path:
    """Refuse a key file's place in the repository or in OneDrive. Reads nothing."""
    p = Path(path).resolve()
    if p.is_relative_to(Path(repo_root).resolve()):
        raise KeyFileRefused(f"{p} is inside the repository; keep the key outside it")
    if any("onedrive" in part.lower() for part in p.parts):
        raise KeyFileRefused(f"{p} is in OneDrive, which would upload it; keep the key outside it")
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and p.is_relative_to(Path(root).resolve()):
            raise KeyFileRefused(f"{p} is in OneDrive, which would upload it; keep the key outside it")
    return p


def _check_readable_only_by_me(p: Path, acl: Callable[[Path], list[str]], me: Callable[[], str]) -> Path:
    if not p.is_file():
        raise KeyFileRefused(f"no key file at {p}")
    allowed = SAFE_PRINCIPALS | {me().lower(), me()}
    others = [w for w in acl(p) if w not in allowed and w.lower() not in allowed]
    if others:
        raise KeyFileRefused(f"{p} can be read by {others}; only your own account should be able to")
    return p


def check_key_path(path: Path | str, repo_root: Path, acl: Callable[[Path], list[str]] = read_acl,
                   me: Callable[[], str] = current_user) -> Path:
    """Refuse a key file in the repository, in OneDrive or readable by others, or the mainnet key. Reads no contents."""
    p = check_key_location(path, repo_root)
    if p.name.lower() in MAINNET_KEY_NAMES:
        raise KeyFileRefused(f"{p.name} is the mainnet key's name; a testnet key has its own file")
    return _check_readable_only_by_me(p, acl, me)


def check_mainnet_key_name(path: Path | str) -> None:
    if Path(path).name.lower() not in MAINNET_KEY_NAMES:
        raise KeyFileRefused(f"{Path(path).name} is not the mainnet bot key's file; only {sorted(MAINNET_KEY_NAMES)}")


def check_mainnet_key_path(path: Path | str, repo_root: Path, acl: Callable[[Path], list[str]] = read_acl,
                           me: Callable[[], str] = current_user) -> Path:
    """Only `xrpl-bot.key`, outside the repository and OneDrive, readable only by Ethan. Reads no contents."""
    check_mainnet_key_name(path)
    return _check_readable_only_by_me(check_key_location(path, repo_root), acl, me)
