"""M7 step 3: the live RLUSD/XRP monitor. READ-ONLY: no keys, no wallet, no orders.

    python scripts/xrpl_monitor.py run    [--db PATH] [--for SECONDS] [--save-raw DIR] [--network testnet]
                                          [--exclude-account rADDRESS]
    python scripts/xrpl_monitor.py status [--db PATH] [--hours H] [--network testnet]

`run` subscribes to validated ledgers and to transactions touching the RLUSD/XRP book and
the AMM pool. On every validated ledger it reads both sides of the book (a shallow read;
a deep one every N ledgers) and the pool *at that ledger*, and stores a snapshot. Trades
come from validated transaction metadata. Outages (disconnected, no validated ledger for
30 s, ledgers missed, monitor not running) are recorded with their reasons. It fails over
between endpoints with backoff and keeps going until Ctrl+C (or --for).

The database defaults to data/xrpl.sqlite3; for multi-day runs put it outside OneDrive
(RUNBOOK: Multi-day runs on Windows). Set DEBTLAB_XRPL_DB or pass --db.

`--network testnet` (M8) watches testnet instead, using data/xrpl_testnet.sqlite3 or --db,
and never DEBTLAB_XRPL_DB, which is the mainnet monitor's. A database records one network:
any other is refused. Testnet prices aren't real, so it shows plumbing and never profit.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config, load_testnet_config, store  # noqa: E402
from debtlab.xrpl_market.live import guards  # noqa: E402
from debtlab.xrpl_market.monitor import KeepAwake, Monitor, now, status  # noqa: E402
from debtlab.xrpl_market.view import open_readonly  # noqa: E402

ADDRESS = re.compile(r"r[1-9A-HJ-NP-Za-km-z]{24,34}")


def default_db() -> Path:
    return Path(os.getenv("DEBTLAB_XRPL_DB") or ROOT / "data" / "xrpl.sqlite3")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--db", type=Path, default=None)
    r.add_argument("--network", choices=("mainnet", "testnet"), default="mainnet")
    r.add_argument("--for", dest="seconds", type=float, help="stop after this many seconds")
    r.add_argument("--save-raw", type=Path, help="save raw transaction messages that contain trades (for fixtures)")
    r.add_argument("--exclude-account", help="leave this account's offers out of the prices the bot quotes from (the bot's own)")
    s = sub.add_parser("status")
    s.add_argument("--db", type=Path, default=None)
    s.add_argument("--network", choices=("mainnet", "testnet"), default="mainnet")
    s.add_argument("--hours", type=float, help="only the last H hours")
    args = ap.parse_args(argv)
    testnet = args.network == "testnet"
    cfg = load_testnet_config() if testnet else load_config()
    if testnet:
        guards.check_config(cfg)
    db = args.db or (ROOT / "data" / "xrpl_testnet.sqlite3" if testnet else default_db())
    exclude = getattr(args, "exclude_account", None)
    if exclude and not ADDRESS.fullmatch(exclude):
        print(f"Refused: {exclude} isn't an XRPL address")
        return 2
    if args.cmd == "status" and Path(db).exists():      # only reads: never migrates a database a monitor is writing
        conn = open_readonly(Path(db))
        have = store.network(conn)
        if have is not None and have != args.network:
            print(f"Refused: this database records {have}, not {args.network}; use a separate database for each network")
            return 2
        if testnet:
            print("TESTNET: test funds with no value; prices here aren't real. READ-ONLY.")
        status(conn, args.hours)
        return 0
    conn = store.open_db(db)
    try:
        store.claim_network(conn, args.network)
    except store.WrongDatabase as e:
        print(f"Refused: {e}")
        return 2
    if testnet:
        print("TESTNET: test funds with no value; prices here aren't real. READ-ONLY.")
    if args.cmd == "status":
        status(conn, args.hours)
        return 0
    until = now() + timedelta(seconds=args.seconds) if args.seconds else None
    with KeepAwake():
        try:
            asyncio.run(Monitor(conn, cfg, until=until, save_raw=args.save_raw, exclude=exclude).run())
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
