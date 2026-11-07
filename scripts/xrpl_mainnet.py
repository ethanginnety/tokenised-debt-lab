"""M8: the RLUSD/XRP bot on the XRPL MAINNET. REAL FUNDS, about EUR 50. Plan approved by Ethan, 7 Nov 2026.

    python scripts/xrpl_mainnet.py keygen [--key-file PATH]   (makes the bot key; prints only its address)
    python scripts/xrpl_mainnet.py check  --key-file PATH     (every start-up check; sends nothing)
    python scripts/xrpl_mainnet.py status
    python scripts/xrpl_mainnet.py run    --key-file PATH [--for SECONDS] [--shadow]
    python scripts/xrpl_mainnet.py resume --reason WHY       (it always starts paused)
    python scripts/xrpl_mainnet.py pause  --reason WHY
    python scripts/xrpl_mainnet.py kill   --key-file PATH --reason WHY    (works with the bot down)
    python scripts/xrpl_mainnet.py reset  --reason WHY       (kill switch off; leaves it paused)
    python scripts/xrpl_mainnet.py limit  NAME VALUE --reason WHY
    python scripts/xrpl_mainnet.py compare
    every command but keygen also takes [--db LIVE_DB] [--paper-db PAPER_DB] [--monitor-db MONITOR_DB] [--by NAME]

The bot signs only with the account's regular key, from `xrpl-bot.key` (RUNBOOK: M8 mainnet).
`run --shadow` reads and reconciles the real account, on its own database, and sends nothing.
Nothing runs or resumes after the hard stop, 11 Nov 2026 20:00 UTC (`live.stop_at`).
`run` needs the mainnet monitor running with --exclude-account set to the bot's account.
The first order needs Ethan's go; he starts the bot and resumes it himself.

The key file's path is only ever given here. The key is never printed, logged or stored.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_mainnet_config  # noqa: E402
from debtlab.xrpl_market.live import book, compare, controls, guards  # noqa: E402
from debtlab.xrpl_market.live import db as ldb  # noqa: E402
from debtlab.xrpl_market.live.alongside import Alongside, open_alongside_paper  # noqa: E402
from debtlab.xrpl_market.live.bot import LiveBot  # noqa: E402
from debtlab.xrpl_market.live.guards import KeyFileRefused, MainnetBlocked  # noqa: E402
from debtlab.xrpl_market.live.ledger import MainnetLedger  # noqa: E402
from debtlab.xrpl_market.live.mainnet_signer import (  # noqa: E402
    ShadowSubmitter, generate_bot_key, open_mainnet_cancel_signer, open_mainnet_signer)
from debtlab.xrpl_market.live.risk import own_offers_problem  # noqa: E402
from debtlab.xrpl_market.live.setup import SetupRefused  # noqa: E402
from debtlab.xrpl_market.live.signer import Submitter  # noqa: E402
from debtlab.xrpl_market.live.venue import LiveVenue  # noqa: E402
from debtlab.xrpl_market.monitor import KeepAwake  # noqa: E402
from debtlab.xrpl_market.paper import controls as pcontrols  # noqa: E402
from debtlab.xrpl_market.paper import db as pdb  # noqa: E402
from debtlab.xrpl_market.paper.market import read_market  # noqa: E402
from debtlab.xrpl_market.readonly import XrplReadError  # noqa: E402
from debtlab.xrpl_market.view import open_readonly  # noqa: E402

DATA = Path("C:/debtlab-data")
DEFAULT_DB = DATA / "xrpl_mainnet_live.sqlite3"
DEFAULT_SHADOW_DB = DATA / "xrpl_mainnet_shadow.sqlite3"
DEFAULT_PAPER_DB = DATA / "xrpl_mainnet_paper.sqlite3"
DEFAULT_MONITOR_DB = DATA / "xrpl.sqlite3"
DEFAULT_KEY = Path("C:/debtlab-secrets/xrpl-bot.key")
NOT_RUNNING_AFTER_SECONDS = 30
LABEL = guards.MAINNET.label
SHADOW = "SHADOW RUN, nothing is sent"
KEY_CHECKS: dict = {}       # who may read the key file: the real checks, unless a test replaces them


def now() -> datetime:
    return datetime.now(timezone.utc)


def lock_folder(folder: Path) -> None:
    """Only Ethan's Windows account can open the folder, and files made in it inherit that. Reads no file."""
    if sys.platform != "win32":
        folder.chmod(0o700)
        return
    me = guards.current_user()
    subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"{me}:(OI)(CI)F"], check=True,
                   capture_output=True, text=True)


def stop_at(cfg: dict) -> datetime:
    return datetime.fromisoformat(cfg["live"]["stop_at"].replace("Z", "+00:00"))


def parts(key_file: Path, cfg: dict, shadow: bool = False):
    """The signer and connections. Tests replace the pieces. The signer opens only with the account's regular key."""
    ledger = MainnetLedger(cfg)
    signer = open_mainnet_signer(key_file, cfg, account=cfg["live"]["account"], regular_key=ledger.regular_key)
    urls = cfg["live"]["endpoints"]["websocket"]
    submitter = ShadowSubmitter(*urls) if shadow else Submitter(*urls, net=guards.MAINNET)
    return signer, submitter, ledger, LiveVenue(signer, submitter, cfg)


def cancel_parts(key_file: Path, cfg: dict):
    """For the kill switch: a signer that can only cancel, opened without the regular key read."""
    signer = open_mainnet_cancel_signer(key_file, cfg, account=cfg["live"]["account"])
    submitter = Submitter(*cfg["live"]["endpoints"]["websocket"], net=guards.MAINNET)
    return signer, submitter, MainnetLedger(cfg), LiveVenue(signer, submitter, cfg)


def fee_drops(monitor_db: Path, cfg: dict) -> int:
    fallback = int(cfg["paper"]["network_fallback"]["fee_drops"])
    if not monitor_db.exists():
        return fallback
    try:
        return int(read_market(open_readonly(monitor_db), now(), cfg).fee_xrp * Decimal(1_000_000))
    except Exception:
        return fallback


def liveness(conn, at: datetime) -> str:
    hb = conn.execute("SELECT at FROM live_heartbeat WHERE id = 1").fetchone()
    if hb is None:
        return "NOT RUNNING: it has never run"
    run = conn.execute("SELECT stopped_at, stop_reason FROM live_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run and run["stopped_at"]:
        return f"NOT RUNNING: stopped at {run['stopped_at']} ({run['stop_reason']})"
    age = (at - datetime.fromisoformat(hb["at"])).total_seconds()
    if age > NOT_RUNNING_AFTER_SECONDS:
        return (f"NOT RUNNING: last alive {hb['at']}, {age:.0f} s ago, with no clean stop. "
                "Nothing is quoting or cancelling; offers it left expire by themselves.")
    return f"last alive: {hb['at']} ({age:.0f} s ago)"


def status(conn, monitor_db: Path, cfg: dict) -> str:
    st = ldb.state(conn)
    left = stop_at(cfg) - now()
    lines = [LABEL, liveness(conn, now()), f"state: {st['state']} ({st['reason']})",
             f"hard stop: {cfg['live']['stop_at']}" + (f" ({left.total_seconds() / 3600:.1f} h left)"
                                                      if left.total_seconds() > 0 else " (PASSED)")]
    a = book.account(conn)
    if not a:
        return "\n".join(lines + ["account: not opened yet"])
    xrp, rlusd = book.balances(conn)
    lines.append(f"account: {a['address']}  budget {xrp} XRP, {rlusd} RLUSD")
    aside_x, aside_r = book.set_aside(conn)
    if aside_x or aside_r:
        lines.append(f"set aside, over the budget and not used: {aside_x} XRP, {aside_r} RLUSD")
    mid = None
    if monitor_db.exists():
        m = read_market(open_readonly(monitor_db), now(), cfg)
        mid = m.mid
        problems = m.problems(ldb.limits(conn).get("max_data_age_seconds", Decimal(30)))
        if (why := own_offers_problem(m, a["address"])):
            problems.append(why)
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
    ap.add_argument("--db", type=Path)
    ap.add_argument("--paper-db", type=Path, default=DEFAULT_PAPER_DB)
    ap.add_argument("--monitor-db", type=Path, default=DEFAULT_MONITOR_DB)
    ap.add_argument("--by", default="Ethan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen").add_argument("--key-file", type=Path, default=DEFAULT_KEY)
    sub.add_parser("check").add_argument("--key-file", type=Path, required=True)
    sub.add_parser("status")
    sub.add_parser("compare")
    r = sub.add_parser("run")
    r.add_argument("--key-file", type=Path, required=True)
    r.add_argument("--for", dest="seconds", type=float)
    r.add_argument("--shadow", action="store_true", help="read and reconcile the real account; send nothing")
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

    if args.cmd == "keygen":
        return _keygen(args.key_file)
    cfg = load_mainnet_config()
    try:
        guards.check_mainnet_config(cfg)
    except MainnetBlocked as e:
        print(f"{LABEL}: refused: {e}")
        return 2
    if not cfg["live"]["account"]:
        print(f"{LABEL}: refused: no account in config/xrpl_mainnet.json yet (Claude sets it from the address you give)")
        return 2
    shadow = args.cmd == "run" and args.shadow
    args.db = args.db or (DEFAULT_SHADOW_DB if shadow else DEFAULT_DB)
    try:
        conn = ldb.open_db(args.db, network="mainnet")
        ldb.seed_limits(conn, cfg, now())
        paper = open_alongside_paper(args.paper_db, "mainnet")
        pdb.seed_limits(paper, cfg, now())
        return _command(args, cfg, conn, paper)
    except (ldb.ControlError, ldb.WrongDatabase, FileNotFoundError, SetupRefused) as e:
        print(f"{LABEL}: refused: {e}")
        return 2
    except KeyFileRefused as e:
        print(f"{LABEL}: key file refused: {e}")
        return 2
    except (MainnetBlocked, XrplReadError) as e:
        print(f"{LABEL}: refused: {e}")
        return 2


def _keygen(path: Path) -> int:
    """The bot key straight into its file, the folder locked first; only the address is printed."""
    try:
        guards.check_mainnet_key_name(path)
        p = guards.check_key_location(path, ROOT)
        if p.exists():
            raise KeyFileRefused(f"{p} already exists; a key is never overwritten")
        p.parent.mkdir(parents=True, exist_ok=True)
        lock_folder(p.parent)
        address = generate_bot_key(p, repo_root=ROOT)
        guards.check_mainnet_key_path(p, ROOT, **KEY_CHECKS)
    except KeyFileRefused as e:
        print(f"{LABEL}: key file refused: {e}")
        return 2
    except subprocess.CalledProcessError as e:
        print(f"{LABEL}: couldn't lock the folder ({e.returncode}); no key was made")
        return 2
    print(f"The bot key is in {p}, readable only by your Windows account.")
    print(f"Its address (public, safe to share): {address}")
    print("In Xaman, set this address as the account's regular key (RUNBOOK). Never open or copy the file.")
    return 0


def _command(args, cfg: dict, conn, paper) -> int:
    if args.cmd == "status":
        print(status(conn, args.monitor_db, cfg))
        return 0
    if args.cmd == "compare":
        print(compare.render(compare.compare(conn, paper), compare.fees(conn, paper), label=LABEL))
        return 0
    if args.cmd == "limit":
        changed = ldb.set_limit(conn, args.name, args.value, args.by, args.reason, now())
        print(f"{LABEL}: {args.name} = {args.value}" + ("" if changed else " (unchanged)"))
        return 0
    if args.cmd == "check":
        return _check(args, cfg, conn)
    if args.cmd == "resume":
        if now() >= stop_at(cfg):
            print(f"{LABEL}: refused: the hard stop ({cfg['live']['stop_at']}) has passed")
            return 2
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


def _check(args, cfg: dict, conn) -> int:
    """The signer opened with every check, and the start-up checks at the latest ledger. Sends nothing."""
    signer, submitter, ledger, venue = parts(args.key_file, cfg, shadow=True)
    bot = LiveBot(conn, args.monitor_db, cfg, ledger, venue, submitter, lambda m, n: {}, out=lambda s: None)
    ledger.check_network()
    found = bot.preflight(now())
    m, _ = bot._market(now())
    if (why := own_offers_problem(m, signer.address)):
        found.append(why)
    h = ledger.latest()
    st = ledger.account(signer.address, h.index)
    print(f"{LABEL}: check at ledger {h.index}, sending nothing")
    print(f"account {signer.address}: {st.xrp} XRP, {st.rlusd} RLUSD, {len(st.offers)} offer(s), owner count {st.owner_count}")
    print(f"bot key {signer.key_address}: the account's regular key on the ledger, not its master key")
    lim = ldb.limits(conn)
    print("limits: " + ", ".join(f"{k} {lim[k]}" for k in ldb.LIMITS if k in lim)
          + f"; offers live at most {cfg['offer_expiry']['max_live_seconds']} s; hard stop {cfg['live']['stop_at']}")
    if found:
        for f in found:
            print(f"  PROBLEM: {f}")
        return 2
    print("all checks passed")
    return 0


def _market_or_none(monitor_db: Path, cfg: dict):
    return read_market(open_readonly(monitor_db), now(), cfg) if monitor_db.exists() else None


def _kill(args, cfg: dict, conn, paper) -> int:
    """The state is committed first, so a running bot stops whatever happens to the key or the network."""
    controls.set_killed(conn, args.by, args.reason, now())
    pcontrols.kill(paper, args.by, args.reason, now(), _market_or_none(args.monitor_db, cfg))
    print(f"{LABEL}: kill switch ON. Offers can't be replaced, and each expires by itself within "
          f"{cfg['offer_expiry']['max_live_seconds']} s.")
    try:
        _, submitter, ledger, venue = cancel_parts(args.key_file, cfg)
        r = controls.kill(conn, ledger, venue, cfg, args.by, args.reason, fee_drops(args.monitor_db, cfg),
                          clock=now, sleep=time.sleep)
    except (KeyFileRefused, MainnetBlocked, XrplReadError) as e:
        print(f"{LABEL}: couldn't cancel: {e}")
        return 1
    submitter.close()
    if r.zero:
        print(f"{LABEL}: no offers on the ledger ({r.sent} cancel(s) sent).")
        return 0
    print(f"{LABEL}: {r.remaining} offer(s) still listed after retrying ({r.last_error}). They can't execute once "
          "expired. Run kill again, or check the account on a mainnet explorer.")
    return 1


def _run(args, cfg: dict, conn, paper) -> int:
    if now() >= stop_at(cfg):
        print(f"{LABEL}: refused: the hard stop ({cfg['live']['stop_at']}) has passed")
        return 2
    signer, submitter, ledger, venue = parts(args.key_file, cfg, shadow=args.shadow)
    if args.shadow:
        # No paper and no quotes: it reads and reconciles the real account, and every submit is refused unsent.
        bot = LiveBot(conn, args.monitor_db, cfg, ledger, venue, submitter, lambda m, n: {},
                      out=lambda s: print(f"{SHADOW}: {s}"))
    else:
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
