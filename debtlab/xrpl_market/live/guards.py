"""What stops the live venue reaching mainnet or mishandling a key (M8). No signing here.

Every check raises rather than returns False, so a forgotten `if` can't let anything past.
The allowlists are constants on purpose: going to mainnet takes a code change that fails
tests/test_xrpl_testnet_safety.py, and a separate plan approved by Ethan.

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
from pathlib import Path
from urllib.parse import urlsplit

TESTNET_NETWORK_ID = 1
TESTNET_HOSTS = frozenset({"testnet.xrpl-labs.com", "s.altnet.rippletest.net"})
TESTNET_ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"
ENDPOINT_SCHEMES = frozenset({"wss", "https"})
ENDPOINT_PORTS = frozenset({None, 443, 51233, 51234})
MAINNET_KEY_NAMES = frozenset({"xrpl-bot.key"})     # the RUNBOOK's name for the mainnet bot key
SAFE_PRINCIPALS = frozenset({"NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"})


class MainnetBlocked(Exception):
    """Something would have reached, or could be replayed on, a network other than testnet."""


class KeyFileRefused(Exception):
    """The key file is somewhere it shouldn't be, or can be read by others. Names the path, never the contents."""


def check_endpoint(url: str) -> str:
    """Only an allowlisted testnet host, over TLS. Everything else, mainnet included, is refused."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError as e:
        raise MainnetBlocked(f"endpoint {url!r} can't be read: {e}") from None
    if u.scheme not in ENDPOINT_SCHEMES or (u.hostname or "").lower() not in TESTNET_HOSTS or port not in ENDPOINT_PORTS:
        raise MainnetBlocked(f"{url!r} is not an allowed testnet endpoint; only {sorted(TESTNET_HOSTS)}")
    if u.username or u.password:
        raise MainnetBlocked(f"{url!r}: credentials in an endpoint are refused")
    return url


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
    for urls in cfg["endpoints"].values():
        for url in urls:
            check_endpoint(url)


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


def check_key_path(path: Path | str, repo_root: Path, acl: Callable[[Path], list[str]] = read_acl,
                   me: Callable[[], str] = current_user) -> Path:
    """Refuse a key file in the repository, in OneDrive or readable by others. Reads no contents."""
    p = Path(path).resolve()
    if p.is_relative_to(Path(repo_root).resolve()):
        raise KeyFileRefused(f"{p} is inside the repository; keep the key outside it")
    if any("onedrive" in part.lower() for part in p.parts):
        raise KeyFileRefused(f"{p} is in OneDrive, which would upload it; keep the key outside it")
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and p.is_relative_to(Path(root).resolve()):
            raise KeyFileRefused(f"{p} is in OneDrive, which would upload it; keep the key outside it")
    if p.name.lower() in MAINNET_KEY_NAMES:
        raise KeyFileRefused(f"{p.name} is the mainnet key's name; a testnet key has its own file")
    if not p.is_file():
        raise KeyFileRefused(f"no key file at {p}")
    allowed = SAFE_PRINCIPALS | {me().lower(), me()}
    others = [w for w in acl(p) if w not in allowed and w.lower() not in allowed]
    if others:
        raise KeyFileRefused(f"{p} can be read by {others}; only your own account should be able to")
    return p
