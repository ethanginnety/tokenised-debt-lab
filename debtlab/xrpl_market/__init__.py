"""RLUSD/XRP on the XRP Ledger: read-only market data and paper trading (M7), the testnet venue (M8).

Everything is READ-ONLY except `live.signer`, which is the only module that holds a key,
signs or submits, and only on testnet. `readonly.ReadOnlyClient` only sends public read
methods, and tests/test_xrpl_no_signing.py fails if any other module imports xrpl-py's
signing code.
"""

import json
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "xrpl.json"
TESTNET_CONFIG_FILE = CONFIG_FILE.with_name("xrpl_testnet.json")


def load_config(path: Path = CONFIG_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_testnet_config(path: Path = TESTNET_CONFIG_FILE, base: Path = CONFIG_FILE) -> dict:
    """The testnet config, with the monitor settings and strategy from the main config, so
    the strategy has one source: testnet runs on whatever values are frozen there."""
    main, cfg = load_config(base), load_config(path)
    return {**cfg, "monitor": main["monitor"], "paper": main["paper"], "known_lookalikes": cfg["refused_issuers"]}
