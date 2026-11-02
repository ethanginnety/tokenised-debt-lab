"""RLUSD/XRP on the XRP Ledger: read-only market data and paper trading (M7), the testnet venue (M8).

Everything is READ-ONLY except `live.signer`, which is the only module that holds a key,
signs or submits, and only on testnet. `readonly.ReadOnlyClient` only sends public read
methods, and tests/test_xrpl_no_signing.py fails if any other module imports xrpl-py's
signing code.
"""

import json
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "xrpl.json"


def load_config(path: Path = CONFIG_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
