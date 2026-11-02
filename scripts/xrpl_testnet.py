"""M8: the RLUSD/XRP bot on the XRPL TESTNET. Test funds with no value; it can't sign for mainnet.

    python scripts/xrpl_testnet.py status
    python scripts/xrpl_testnet.py run    --key-file PATH [--for SECONDS]
    python scripts/xrpl_testnet.py resume --reason WHY       (it always starts paused)
    python scripts/xrpl_testnet.py pause  --reason WHY
    python scripts/xrpl_testnet.py kill   --key-file PATH --reason WHY    (works with the bot down)
    python scripts/xrpl_testnet.py reset  --reason WHY       (kill switch off; leaves it paused)
    python scripts/xrpl_testnet.py limit  NAME VALUE --reason WHY
    python scripts/xrpl_testnet.py compare
    every command also takes [--db LIVE_DB] [--paper-db PAPER_DB] [--monitor-db MONITOR_DB] [--by NAME]

`run` needs the testnet monitor running (scripts/xrpl_monitor.py run --network testnet) and
only reads its database. RUNBOOK: M8 testnet. The first order needs Ethan's go; he starts it.

The key file's path is only ever given here. It is never printed, logged or stored.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config, load_testnet_config  # noqa: E402
from debtlab.xrpl_market.live import LABEL, book, compare, controls  # noqa: E402
from debtlab.xrpl_market.live import db as ldb  # noqa: E402
from debtlab.xrpl_market.live.alongside import Alongside, open_testnet_paper  # noqa: E402
from debtlab.xrpl_market.live.bot import LiveBot  # noqa: E402
from debtlab.xrpl_market.live.guards import KeyFileRefused, MainnetBlocked  # noqa: E402
from debtlab.xrpl_market.live.ledger import TestnetLedger, exists_on_mainnet  # noqa: E402
from debtlab.xrpl_market.live.signer import Submitter, open_signer  # noqa: E402
from debtlab.xrpl_market.live.venue import LiveVenue  # noqa: E402
from debtlab.xrpl_market.monitor import KeepAwake  # noqa: E402
from debtlab.xrpl_market.paper import controls as pcontrols  # noqa: E402
from debtlab.xrpl_market.paper import db as pdb  # noqa: E402
from debtlab.xrpl_market.paper.market import read_market  # noqa: E402
from debtlab.xrpl_market.readonly import XrplReadError  # noqa: E402
from debtlab.xrpl_market.view import open_readonly  # noqa: E402

DEFAULT_DB = ROOT / "data" / "xrpl_live.sqlite3"
DEFAULT_PAPER_DB = ROOT / "data" / "xrpl_testnet_paper.sqlite3"
DEFAULT_MONITOR_DB = ROOT / "data" / "xrpl_testnet.sqlite3"


def now() -> datetime:
    return datetime.now(timezone.utc)


def parts(key_file: Path, cfg: dict):
    """The signer and connections. Tests replace this. The signer refuses a key whose account is on mainnet."""
    mainnet = load_config()["endpoints"]["json_rpc"]
    signer = open_signer(key_file, cfg, on_mainnet=lambda a: exists_on_mainnet(a, mainnet))
    submitter = Submitter(cfg["endpoints"]["websocket"][0])
    return signer, submitter, TestnetLedger(cfg), LiveVenue(signer, submitter, cfg)


def fee_drops(monitor_db: Path, cfg: dict) -> int:
    fallback = int(cfg["paper"]["network_fallback"]["fee_drops"])
    if not monitor_db.exists():
        return fallback
    try:
        return int(read_market(open_readonly(monitor_db), now(), cfg).fee_xrp * Decimal(1_000_000))
    except Exception:
        return fallback


def status(conn, monitor_db: Path, cfg: dict) -> str:
    st = ldb.state(conn)
    lines = [f"{LABEL}", f"state: {st['state']} ({st['reason']})"]
    a = book.account(conn)
    if not a:
        return "\n".join(lines + ["account: not opened yet"])
    xrp, rlusd = book.balances(conn)
    lines.append(f"account: {a['address']}  {xrp} XRP, {rlusd} RLUSD in the books")
    hb = conn.execute("SELECT at FROM live_heartbeat WHERE id = 1").fetchone()
    lines.append(f"last alive: {hb['at'] if hb else 'never'}")
    mid = None
    if monitor_db.exists():
        m = read_market(open_readonly(monitor_db), now(), cfg)
        mid = m.mid
        problems = m.problems(ldb.limits(conn).get("max_data_age_seconds", Decimal(30)))
        lines.append("market: " + (f"mid {m.mid}" if m.mid else "no price") + ("; " + "; ".join(problems) if problems else ""))
    p = book.pnl(conn, mid if mid is not None else book.last_mid(conn))
    lines.append(f"P&L, RLUSD: total {p.total} = spread {p.spread} + fees {p.fees} + inventory {p.inventory}; "
                 f"{p.maker_fills} maker fill(s), {p.taker_fills} that took liquidity; outside money {p.external}")
    for o in book.open_orders(conn):
        lines.append(f"  {o['status']}: {o['side']} {o['remaining']} XRP at {o['price']}, sequence {o['sequence']}, expires {o['expiration']}")
    for r in conn.execute("SELECT at, kind, detail FROM live_events WHERE kind IN ('order_refused', 'connection_lost',"
                          " 'tx_not_applied', 'cancel_incomplete', 'taker_fill', 'external_change', 'state_changed')"
                          " ORDER BY id DESC LIMIT 5"):
        lines.append(f"  {r['at']} {r['kind']}: {r['detail'][:200]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--paper-db", type=Path, default=DEFAULT_PAPER_DB)
    ap.add_argument("--monitor-db", type=Path, default=DEFAULT_MONITOR_DB)
    ap.add_argument("--by", default="Ethan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("compare")
    r = sub.add_parser("run")
    r.add_argument("--key-file", type=Path, required=True)
    r.add_argument("--for", dest="seconds", type=float)
    for name in ("resume", "pause", "reset"):
        sub.add_parser(name).add_argument("--reason", required=True)
    k = sub.add_parser("kill")
    k.add_argument("--key-file", type=Path, required=True)
    k.add_argument("--reason", required=True)
    lim = sub.add_parser("limit")
    lim.add_argument("name", choices=sorted(ldb.LIMITS))
    lim.add_argument("value", type=Decimal)
    lim.add_argument("--reason", required=True)
    args = ap.parse_args(argv)

    cfg = load_testnet_config()
    conn = ldb.open_db(args.db)
    ldb.seed_limits(conn, cfg, now())
    paper = open_testnet_paper(args.paper_db)
    pdb.seed_limits(paper, cfg, now())
    try:
        return _command(args, cfg, conn, paper)
    except ldb.ControlError as e:
        print(f"{LABEL}: refused: {e}")
        return 2
    except (ldb.WrongDatabase, FileNotFoundError) as e:
        print(f"{LABEL}: refused: {e}")
        return 2
    except KeyFileRefused as e:
        print(f"{LABEL}: key file refused: {e}")
        return 2
    except (MainnetBlocked, XrplReadError) as e:
        print(f"{LABEL}: refused: {e}")
        return 2


def _command(args, cfg: dict, conn, paper) -> int:
    if args.cmd == "status":
        print(status(conn, args.monitor_db, cfg))
        return 0
    if args.cmd == "compare":
        print(compare.render(compare.compare(conn, paper), compare.fees(conn, paper)))
        return 0
    if args.cmd == "limit":
        changed = ldb.set_limit(conn, args.name, args.value, args.by, args.reason, now())
        print(f"{LABEL}: {args.name} = {args.value}" + ("" if changed else " (unchanged)"))
        return 0
    if args.cmd == "resume":
        controls.resume(conn, args.by, args.reason, now())     # refuses first if the kill switch is on
        pcontrols.resume(paper, args.by, args.reason, now())
    elif args.cmd == "pause":
        controls.pause(conn, args.by, args.reason, now())
        print(f"{LABEL}: a running bot cancels its offers on its next tick; with the bot down they expire by themselves")
    elif args.cmd == "reset":
        controls.reset(conn, args.by, args.reason, now())
        if pdb.state(paper)["state"] == "killed":
            pcontrols.reset(paper, args.by, args.reason, now())
    elif args.cmd == "kill":
        return _kill(args, cfg, conn, paper)
    elif args.cmd == "run":
        return _run(args, cfg, conn, paper)
    print(f"{LABEL}: state is now {ldb.state(conn)['state']}")
    return 0


def _market_or_none(monitor_db: Path, cfg: dict):
    return read_market(open_readonly(monitor_db), now(), cfg) if monitor_db.exists() else None


def _kill(args, cfg: dict, conn, paper) -> int:
    """The state is committed first, so a running bot stops whatever happens to the key or the network."""
    controls.set_killed(conn, args.by, args.reason, now())
    pcontrols.kill(paper, args.by, args.reason, now(), _market_or_none(args.monitor_db, cfg))
    print(f"{LABEL}: kill switch ON. Offers can't be replaced, and each expires by itself within 30 s.")
    try:
        _, submitter, ledger, venue = parts(args.key_file, cfg)
        r = controls.kill(conn, ledger, venue, cfg, args.by, args.reason, fee_drops(args.monitor_db, cfg), clock=now, sleep=time.sleep)
    except (KeyFileRefused, MainnetBlocked, XrplReadError) as e:
        print(f"{LABEL}: couldn't cancel: {e}")
        return 1
    submitter.close()
    if r.zero:
        print(f"{LABEL}: no offers on the ledger ({r.sent} cancel(s) sent).")
        return 0
    print(f"{LABEL}: {r.remaining} offer(s) still listed after retrying ({r.last_error}). They can't execute once expired. "
          "Run kill again, or check the account on a testnet explorer.")
    return 1


def _run(args, cfg: dict, conn, paper) -> int:
    signer, submitter, ledger, venue = parts(args.key_file, cfg)
    side = Alongside(conn, paper, args.monitor_db, cfg)
    bot = LiveBot(conn, args.monitor_db, cfg, ledger, venue, submitter, side.wants, alongside=side)
    until = now() + timedelta(seconds=args.seconds) if args.seconds else None
    try:
        with KeepAwake():
            bot.run(until=until)
    finally:
        submitter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
