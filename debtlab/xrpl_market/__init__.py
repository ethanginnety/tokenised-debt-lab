"""RLUSD/XRP on the XRP Ledger (M7): read-only market data, then paper trading.

READ-ONLY. Nothing in this package holds a key, builds a wallet, signs or submits a
transaction. `readonly.ReadOnlyClient` only sends public read methods, and
tests/test_xrpl_no_signing.py fails if any module imports xrpl-py's signing code.
"""

import json
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "xrpl.json"


def load_config(path: Path = CONFIG_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
