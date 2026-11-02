"""M7 steps 5-6: the RLUSD/XRP paper bot. PAPER: no key, no wallet, no order on any ledger.

    python scripts/xrpl_paper.py status
    python scripts/xrpl_paper.py run    [--for SECONDS]
    python scripts/xrpl_paper.py resume --reason WHY      (the bot starts paused)
    python scripts/xrpl_paper.py pause  --reason WHY
    python scripts/xrpl_paper.py kill   --reason WHY      (stays on across restarts)
    python scripts/xrpl_paper.py reset  --reason WHY      (kill switch off; leaves it paused)
    python scripts/xrpl_paper.py limit  NAME VALUE --reason WHY
    every command also takes [--db PAPER_DB] [--monitor-db MONITOR_DB] [--by NAME]

`run` needs the monitor running (scripts/xrpl_monitor.py run) and only reads its database.
It quotes RLUSD/XRP on paper for two accounts on the same quotes, one per fill rule
(decision E), and cancels everything when the data is older than 30 s or the monitor is
down. The paper run (M7 step 7) starts only after Ethan's go at the checkpoint.

Databases: the paper bot's own (DEBTLAB_XRPL_PAPER_DB, default data/xrpl_paper.sqlite3) and
the monitor's (DEBTLAB_XRPL_DB, default data/xrpl.sqlite3). For multi-day runs keep both
outside OneDrive (RUNBOOK: Multi-day runs on Windows).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config  # noqa: E402
from debtlab.xrpl_market.monitor import KeepAwake  # noqa: E402
from debtlab.xrpl_market.paper import controls, report  # noqa: E402
from debtlab.xrpl_market.paper import db as pdb  # noqa: E402
from debtlab.xrpl_market.paper.bot import PaperBot  # noqa: E402
from debtlab.xrpl_market.paper.market import read_market  # noqa: E402
from debtlab.xrpl_market.view import open_readonly  # noqa: E402


def default_paper_db() -> Path:
    return Path(os.getenv("DEBTLAB_XRPL_PAPER_DB") or ROOT / "data" / "xrpl_paper.sqlite3")


def default_monitor_db() -> Path:
    return Path(os.getenv("DEBTLAB_XRPL_DB") or ROOT / "data" / "xrpl.sqlite3")


def now() -> datetime:
    return datetime.now(timezone.utc)


def market(monitor_db: Path, cfg: dict):
    """The market now, or None if there's no monitor database (pause and kill still work)."""
    if not monitor_db.exists():
        return None, None
    mon = open_readonly(monitor_db)
    return read_market(mon, now(), cfg), mon


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--monitor-db", type=Path, default=None)
    ap.add_argument("--by", default="Ethan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--for", dest="seconds", type=float, help="stop after this many seconds")
    for name in ("resume", "pause", "kill", "reset"):
        sub.add_parser(name).add_argument("--reason", required=True)
    lim = sub.add_parser("limit")
    lim.add_argument("name", choices=sorted(pdb.LIMITS))
    lim.add_argument("value", type=Decimal)
    lim.add_argument("--reason", required=True)
    args = ap.parse_args(argv)

    cfg = load_config()
    conn = pdb.open_db(args.db or default_paper_db())
    mon_db = args.monitor_db or default_monitor_db()
    pdb.seed_limits(conn, cfg, now())
    try:
        if args.cmd == "run":
            until = now() + timedelta(seconds=args.seconds) if args.seconds else None
            with KeepAwake():
                PaperBot(conn, mon_db, cfg).run(until=until)
            return 0
        if args.cmd == "status":
            m, mon = market(mon_db, cfg)
            if m is None:
                print(f"PAPER: no monitor database at {mon_db}; state {pdb.state(conn)}")
                return 1
            print(report.render(report.status(conn, mon, m, now(), cfg)))
            return 0
        if args.cmd == "limit":
            changed = pdb.set_limit(conn, args.name, args.value, args.by, args.reason, now())
            print(f"PAPER: {args.name} = {args.value}" + ("" if changed else " (unchanged)"))
            return 0
        if args.cmd == "resume":
            controls.resume(conn, args.by, args.reason, now())
        elif args.cmd == "reset":
            controls.reset(conn, args.by, args.reason, now())
        else:
            m, _ = market(mon_db, cfg)
            n = (controls.pause if args.cmd == "pause" else controls.kill)(conn, args.by, args.reason, now(), m)
            print(f"PAPER: {n} open paper order(s) cancelled")
        print(f"PAPER: state is now {pdb.state(conn)['state']}")
        return 0
    except pdb.ControlError as e:
        print(f"PAPER: refused: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
