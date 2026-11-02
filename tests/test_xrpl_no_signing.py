"""Only one file may sign, submit or hold a key: `live/signer.py` (M8, testnet, approved by Ethan on 2 Nov 2026).

Scans every Python file the app and scripts are made of. A failure here means someone
added signing or key handling elsewhere; that needs Ethan's approval, not a test change.
Widening `MAY_SIGN` is a test change, so it shows in review. The signer's own rules are in
tests/test_xrpl_testnet_safety.py.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*ROOT.glob("debtlab/**/*.py"), *ROOT.glob("scripts/**/*.py")])
MAY_SIGN = {ROOT / "debtlab" / "xrpl_market" / "live" / "signer.py"}

FORBIDDEN = {
    "xrpl-py wallet": re.compile(r"\bxrpl\.wallet\b|from\s+xrpl\s+import[^\n]*\bwallet\b|\bWallet\s*\("),
    "xrpl-py signing or submitting": re.compile(
        r"\bxrpl\.transaction\b|from\s+xrpl\s+import[^\n]*\btransaction\b|\bxrpl\.core\.keypairs\b"
        r"|\b(submit_and_wait|autofill_and_sign|sign_and_submit|submit_transaction)\b"),
    "a seed or key from the environment": re.compile(
        r"(getenv|environ(\.get)?)\s*[\(\[]\s*['\"][^'\"]*(SEED|SECRET|PRIVATE|MNEMONIC|KEY)[^'\"]*['\"]", re.I),
    "a raw submit request": re.compile(
        r"['\"](method|command)['\"]\s*:\s*['\"](submit|sign|sign_for|submit_multisigned)['\"]"),
    "xrpl-py transaction models": re.compile(r"\bxrpl\.models\.transactions\b|\bxrpl\.models\b.*\btransactions\b"),
}
ENV_KEY = "a seed or key from the environment"


def test_the_scan_sees_the_xrpl_code():
    names = {p.name for p in SOURCES}
    assert {"readonly.py", "verify.py", "xrpl_probe.py", "wsclient.py", "monitor.py", "xrpl_monitor.py",
            "bot.py", "venue.py", "risk.py", "strategy.py", "xrpl_paper.py"} <= names


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_signing_or_key_handling(path):
    text = path.read_text(encoding="utf-8")
    rules = {ENV_KEY: FORBIDDEN[ENV_KEY]} if path in MAY_SIGN else FORBIDDEN    # never a key from the environment
    found = [what for what, rx in rules.items() if rx.search(text)]
    assert not found, f"{path.relative_to(ROOT)}: {found}"


def test_exactly_one_file_may_sign():
    assert [p.relative_to(ROOT).as_posix() for p in MAY_SIGN] == ["debtlab/xrpl_market/live/signer.py"]


@pytest.mark.parametrize("snippet", [
    "from xrpl.wallet import Wallet", "import xrpl.transaction", "from xrpl import wallet",
    "submit_and_wait(tx, client, w)", "os.getenv('XRPL_SEED')", 'os.environ["WALLET_SECRET"]',
    '{"method": "submit", "params": []}', '{"id": 1, "command": "submit", "tx_blob": "00"}',
    "from xrpl.models.transactions import Payment", "import xrpl.models.transactions",
])
def test_the_scan_catches_what_it_should(snippet):
    assert any(rx.search(snippet) for rx in FORBIDDEN.values())
