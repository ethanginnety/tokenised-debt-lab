"""Operate the simulated market maker. SIMULATED: not evidence of real profitability.

    python scripts/mm.py status                          state, holdings, open orders, the day's P&L, limits, recent runs
    python scripts/mm.py run [--seed 7] [--profile normal|stress] [--informed 0.045] [--steps N] [--id RUN]
                                                         one simulated day on the latest official curves
    python scripts/mm.py pause  --reason "lunch"         stop placing orders; open orders are cancelled
    python scripts/mm.py resume --reason "back"          start again (not after a kill: reset first)
    python scripts/mm.py kill   --reason "looks wrong"   kill switch: cancel everything, stay off until reset
    python scripts/mm.py reset  --reason "checked"       turn the kill switch off; the bot is left paused
    python scripts/mm.py limits                          every limit in force, with who set it and why
    python scripts/mm.py limits max_daily_loss GBP 15000 --reason "tighter in stress"
    python scripts/mm.py unwind RUN --reason "after the kill"   unwind a finished run's inventory, on command

Options: --db PATH (default: the main database), --by NAME (default: your login name),
--at TIME (ISO, UTC; the clock to use, default now).

Every change goes through the same functions the bot uses, so it is checked, stored and
audited with who and why. The day's P&L shown by `status` is worked out from the ledger
at the current marks, exactly as the daily-loss limit sees it; `status` writes nothing.
A run that crashed part-way is finished by running it again with the same --id.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debtlab import ledger  # noqa: E402
from debtlab.audit.db import connect, get_db_role, latest_curve, migrate  # noqa: E402
from debtlab.config import Settings  # noqa: E402
from debtlab.instruments import load_instruments  # noqa: E402
from debtlab.marketdata.adapters import to_zero_snapshot  # noqa: E402
from debtlab.marketdata.models import MARKET_KINDS, risk_increasing_allowed  # noqa: E402
from debtlab.marketmaker import controls, daily, simulation  # noqa: E402
from debtlab.marketmaker.book import load_book_config, seed_book  # noqa: E402
from debtlab.marketmaker.quoting import current_marks  # noqa: E402
from debtlab.money import D  # noqa: E402
from debtlab.pricing.bond import FixedRateBond  # noqa: E402

COMMANDS = ("status", "run", "pause", "resume", "kill", "reset", "limits", "unwind")
NEEDS_REASON = ("pause", "resume", "kill", "reset", "unwind")


def main(argv: list[str] | None = None) -> None:
    settings = Settings.from_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("args", nargs="*", help="limits: NAME SCOPE VALUE to change one; unwind: RUN")
    ap.add_argument("--db", type=Path, default=settings.db_path)
    ap.add_argument("--by", default=getpass.getuser())
    ap.add_argument("--reason", default="")
    ap.add_argument("--at", type=_utc, help="the clock, ISO time (default now, UTC)")
    ap.add_argument("--seed", default="1")
    ap.add_argument("--profile", default="normal")
    ap.add_argument("--informed", help="informed arrival chance per bond per step, on top (config default 0)")
    ap.add_argument("--steps", type=int, help="steps in the run (config default: 390, one day)")
    ap.add_argument("--id", help="run id (default from the clock, profile and seed); reuse it to finish a crashed run")
    a = ap.parse_args(argv)

    if a.command in NEEDS_REASON and not a.reason.strip():
        ap.error(f"{a.command} needs --reason")
    if a.command == "limits" and a.args and (len(a.args) != 3 or not a.reason.strip()):
        ap.error("to change a limit: limits NAME SCOPE VALUE --reason WHY")
    if a.command == "unwind" and len(a.args) != 1:
        ap.error("unwind needs a run id: unwind RUN --reason WHY")
    if not a.db.exists():
        sys.exit(f"{a.db} doesn't exist; start the app once to create it")
    now = a.at or datetime.now(timezone.utc)

    conn = connect(a.db)
    try:
        migrate(conn)
        instruments = load_instruments(settings.instruments_file)
        if get_db_role(conn) == "main":  # as the app does at startup; idempotent, and never loosens a limit
            seed_book(conn, load_book_config(settings.marketmaker_file), instruments)
            controls.seed_limits(conn, settings.marketmaker_file)
        if not conn.execute("SELECT 1 FROM parties WHERE role = 'market_maker'").fetchone():
            sys.exit(f"{a.db} has no market maker (create a scenario database with --market-maker)")

        if a.command == "pause":
            n = controls.pause(conn, a.by, a.reason, now)
            print(f"paused; {n} open order(s) cancelled")
        elif a.command == "resume":
            controls.resume(conn, a.by, a.reason, now)
            print("running")
        elif a.command == "kill":
            n = controls.kill(conn, a.by, a.reason, now)
            print(f"KILLED; {n} open order(s) cancelled. Nothing trades until: mm.py reset --reason WHY")
        elif a.command == "reset":
            controls.reset(conn, a.by, a.reason, now)
            print("kill switch off; the bot is paused (resume to run)")
        elif a.command == "limits":
            if a.args:
                name, scope, value = a.args
                before = controls.limits(conn).get((name, scope))
                changed = controls.set_limit(conn, name, scope, D(value), a.by, a.reason, now)
                print(f"{name} {scope}: {before} -> {value}" if changed else f"{name} {scope} is already {value}")
            show_limits(conn)
        elif a.command == "run":
            run(conn, settings, instruments, now, a)
        elif a.command == "unwind":
            curves = official_curves(conn)
            s = simulation.unwind(conn, a.args[0], instruments, curves, now.date(),
                                  settings.marketmaker_file, a.by, a.reason)
            print_summary(a.args[0], s)
        if a.command != "limits":
            status(conn, settings, instruments, now, brief=a.command != "status")
    except (controls.ControlError, simulation.SimulationError) as exc:
        sys.exit(f"refused: {exc}")
    finally:
        conn.close()


def _utc(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def official_curves(conn) -> dict:
    """The latest stored official curve per currency (never illustrative), as zero curves."""
    return {c: to_zero_snapshot(s) for c in ("GBP", "USD") if (s := latest_curve(conn, c, MARKET_KINDS))}


def run(conn, settings: Settings, instruments, now: datetime, a) -> None:
    st = controls.state(conn)
    if st["state"] != "running":
        sys.exit(f"refused: the bot is {st['state']} ({st['reason']}); "
                 f"{'reset, then ' if st['state'] == 'killed' else ''}resume it first")
    curves = official_curves(conn)
    usable = {}
    for ccy, snap in sorted(curves.items()):
        ok, why = risk_increasing_allowed(snap, now, settings.max_curve_age_hours)
        print(f"{ccy} curve: {snap.source}, observed {snap.observation_date}, {snap.age_hours(now):.0f}h old: {why}")
        if ok:
            usable[ccy] = snap
    if not usable:
        sys.exit("refused: no fresh official curve; run the app in online mode to fetch one")
    overrides = {k: v for k, v in (("informed_arrival_prob", a.informed), ("steps", a.steps)) if v is not None}
    params = simulation.load_sim_params(settings.marketmaker_file, a.profile, **overrides)
    run_id = a.id or f"{now:%Y%m%dT%H%M}-{a.profile}-s{a.seed}"
    print(f"{simulation.LABEL}\nrun {run_id}: seed {a.seed}, profile {a.profile}, {params.steps} steps, "
          f"informed {params.informed_arrival_prob} a step, starting {now.isoformat()}")
    s = simulation.run(conn, run_id, a.seed, instruments, curves, now.date(), now,
                       settings.max_curve_age_hours, settings.marketmaker_file, params)
    print_summary(run_id, s)


def print_summary(run_id: str, s: dict) -> None:
    print(f"\n{s['label']}. Run {run_id} ({s['profile']}); unwind: {s['unwind']}; "
          f"bot at the end: {s['bot_end']['state']} ({s['bot_end']['reason']})")
    for inst, x in s["instruments"].items():
        fills = ", ".join(f"{n} {c}" for c, n in x["fills"].items() if n)
        print(f"  {inst:<18} fills: {fills or 'none'}; bought {D(x['bought']):,f}, sold {D(x['sold']):,f}; "
              f"inventory {D(x['inventory_start']):,f} -> {D(x['inventory_end']):,f}; "
              f"fair {x['fair_start']} -> {x['fair_end']} dirty")
    print("  P&L         spread    start MTM   traded MTM      fees  slippage        total  (from ledger)")
    for ccy, p in sorted(s["pnl"].items()):
        print(f"  {ccy}   " + "".join(f"{D(p[k]):>12,.2f}" for k in ("spread", "start_mtm", "traded_mtm", "fees",
                                                                     "slippage", "total"))
              + f"  ({D(p['total_from_ledger']):,.2f})")
    for ccy, by in sorted(s["markouts"].items()):
        for client, m in sorted(by.items()):
            print(f"  markouts {ccy} {client:<10} {m['fills']} fills: "
                  + ", ".join(f"{k.replace('_', ' ')} {D(v):,.2f}" for k, v in m.items() if k != "fills"))


def status(conn, settings: Settings, instruments, now: datetime, brief: bool = False) -> None:
    st = controls.state(conn)
    print(f"\nbot {st['state'].upper()}" + (f" since {st['changed_at']} by {st['changed_by']}: {st['reason']}"
                                           if st["changed_at"] else f" ({st['reason']})"))
    mm = conn.execute("SELECT id FROM parties WHERE role = 'market_maker'").fetchone()["id"]
    held = {k: v for k, v in ledger.balances(conn).get(mm, {}).items() if v}
    print("holdings: " + ", ".join(f"{k} {v:,f}" for k, v in sorted(held.items())))
    orders = conn.execute("SELECT * FROM mm_orders WHERE status = 'open' ORDER BY id").fetchall()
    print(f"open orders: {len(orders)}")
    for o in orders:
        print(f"  {o['id']}  {o['side']} {D(o['size']) - D(o['filled']):,f} {o['instrument_id']} at {o['price']}")

    bonds = [i for i in instruments if isinstance(i, FixedRateBond)]
    marks = current_marks(conn, bonds, official_curves(conn), now.date(), now, settings.max_curve_age_hours)
    day = daily.daily_pnl(conn, marks, now, write=False)
    lim = controls.limits(conn)
    print(f"day {now.date()} (UTC), P&L from the ledger at current marks (what the daily-loss limit sees):")
    if day is None:
        print("  not opened yet: the day opens at the first quote or order")
    else:
        for ccy, v in sorted(day.items()):
            cap = lim.get(("max_daily_loss", ccy))
            shown = "unknown (a holding can't be valued: bids refused)" if v is None else f"{v:,.2f}"
            print(f"  {ccy} {shown}; loss limit {cap:,f}" if cap is not None else f"  {ccy} {shown}; no loss limit")
    if brief:
        return
    show_limits(conn)
    runs = conn.execute("SELECT id, status, started_at, summary FROM mm_runs ORDER BY rowid DESC LIMIT 5").fetchall()
    print(f"recent runs ({simulation.LABEL}):" if runs else "no simulation runs yet")
    for r in runs:
        totals = ""
        if r["summary"]:
            totals = "; total " + ", ".join(f"{c} {D(p['total']):,.2f}" for c, p in
                                           sorted(json.loads(r["summary"])["pnl"].items()))
        print(f"  {r['id']}  {r['status']}  started {r['started_at']}{totals}")
    problems = ledger.check_integrity(conn)
    print("ledger integrity:", "ok" if not problems else problems)


def show_limits(conn) -> None:
    print("limits in force:")
    rows = conn.execute("SELECT * FROM mm_limit_changes WHERE id IN"
                        " (SELECT MAX(id) FROM mm_limit_changes GROUP BY name, scope) ORDER BY name, scope")
    for r in rows:
        unit = controls.LIMITS.get(r["name"], ("", ""))[1]
        print(f"  {r['name']:<20} {r['scope']:<18} {D(r['value']):>14,f} {unit:<13}"
              f" set {r['changed_at'][:16]} by {r['changed_by']}: {r['reason']}")


if __name__ == "__main__":
    main()
