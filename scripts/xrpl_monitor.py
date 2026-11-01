"""M7 step 3: the live RLUSD/XRP monitor. READ-ONLY: no keys, no wallet, no orders.

    python scripts/xrpl_monitor.py run    [--db PATH] [--for SECONDS] [--save-raw DIR]
    python scripts/xrpl_monitor.py status [--db PATH] [--hours H]

`run` subscribes to validated ledgers and to transactions touching the RLUSD/XRP book and
the AMM pool. On every validated ledger it reads both sides of the book (a shallow read;
a deep one every N ledgers) and the pool *at that ledger*, and stores a snapshot. Trades
come from validated transaction metadata. Outages (disconnected, no validated ledger for
30 s, ledgers missed, monitor not running) are recorded with their reasons. It fails over
between endpoints with backoff and keeps going until Ctrl+C (or --for).

The database defaults to data/xrpl.sqlite3; for multi-day runs put it outside OneDrive
(RUNBOOK: Multi-day runs on Windows). Set DEBTLAB_XRPL_DB or pass --db.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config, store  # noqa: E402
from debtlab.xrpl_market.monitor import KeepAwake, Monitor, now, status  # noqa: E402


def default_db() -> Path:
    return Path(os.getenv("DEBTLAB_XRPL_DB") or ROOT / "data" / "xrpl.sqlite3")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--db", type=Path, default=None)
    r.add_argument("--for", dest="seconds", type=float, help="stop after this many seconds")
    r.add_argument("--save-raw", type=Path, help="save raw transaction messages that contain trades (for fixtures)")
    s = sub.add_parser("status")
    s.add_argument("--db", type=Path, default=None)
    s.add_argument("--hours", type=float, help="only the last H hours")
    args = ap.parse_args()
    conn = store.open_db(args.db or default_db())
    if args.cmd == "status":
        status(conn, args.hours)
        return 0
    until = now() + timedelta(seconds=args.seconds) if args.seconds else None
    with KeepAwake():
        try:
            asyncio.run(Monitor(conn, load_config(), until=until, save_raw=args.save_raw).run())
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
