"""M7 is read-only and paper only: no code may sign, submit, or hold a key.

Scans every Python file the app and scripts are made of. A failure here means someone
added signing or key handling; that needs Ethan's approval (M8), not a test change.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*ROOT.glob("debtlab/**/*.py"), *ROOT.glob("scripts/**/*.py")])

FORBIDDEN = {
    "xrpl-py wallet": re.compile(r"\bxrpl\.wallet\b|from\s+xrpl\s+import[^\n]*\bwallet\b|\bWallet\s*\("),
    "xrpl-py signing or submitting": re.compile(
        r"\bxrpl\.transaction\b|from\s+xrpl\s+import[^\n]*\btransaction\b|\bxrpl\.core\.keypairs\b"
        r"|\b(submit_and_wait|autofill_and_sign|sign_and_submit|submit_transaction)\b"),
    "a seed or key from the environment": re.compile(
        r"(getenv|environ(\.get)?)\s*[\(\[]\s*['\"][^'\"]*(SEED|SECRET|PRIVATE|MNEMONIC|KEY)[^'\"]*['\"]", re.I),
    "a raw submit request": re.compile(
        r"['\"](method|command)['\"]\s*:\s*['\"](submit|sign|sign_for|submit_multisigned)['\"]"),
}


def test_the_scan_sees_the_xrpl_code():
    names = {p.name for p in SOURCES}
    assert {"readonly.py", "verify.py", "xrpl_probe.py", "wsclient.py", "monitor.py", "xrpl_monitor.py"} <= names


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_signing_or_key_handling(path):
    text = path.read_text(encoding="utf-8")
    found = [what for what, rx in FORBIDDEN.items() if rx.search(text)]
    assert not found, f"{path.relative_to(ROOT)}: {found}"


@pytest.mark.parametrize("snippet", [
    "from xrpl.wallet import Wallet", "import xrpl.transaction", "from xrpl import wallet",
    "submit_and_wait(tx, client, w)", "os.getenv('XRPL_SEED')", 'os.environ["WALLET_SECRET"]',
    '{"method": "submit", "params": []}', '{"id": 1, "command": "submit", "tx_blob": "00"}',
])
def test_the_scan_catches_what_it_should(snippet):
    assert any(rx.search(snippet) for rx in FORBIDDEN.values())
