"""Run the bond lifecycle in the scenario database.

    python scripts/lifecycle.py status              simulation date, payments, cash balances
    python scripts/lifecycle.py pay                 pay every coupon due by the simulation date
    python scripts/lifecycle.py advance 2027-02-17  move the simulation date forward, then pay what is due

Add --db PATH to use another scenario database (default: data/scenario.sqlite3).
It refuses to run on a database that isn't marked as a scenario.
Running `pay` again is always safe: only amounts still due are paid.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debtlab import ledger, lifecycle  # noqa: E402
from debtlab.audit.db import connect, migrate  # noqa: E402
from debtlab.config import Settings  # noqa: E402
from debtlab.instruments import load_instruments  # noqa: E402
from debtlab.pricing.bond import FixedRateBond  # noqa: E402
from debtlab.scenario import DEFAULT_SCENARIO_DB  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "pay", "advance"))
    parser.add_argument("date", nargs="?", type=date.fromisoformat, help="new simulation date (advance only)")
    parser.add_argument("--db", type=Path, default=DEFAULT_SCENARIO_DB)
    args = parser.parse_args()
    if args.command == "advance" and args.date is None:
        parser.error("advance needs a date, e.g. advance 2027-02-17")
    if not args.db.exists():
        sys.exit(f"{args.db} doesn't exist; create it with: python scripts/new_scenario_db.py")

    conn = connect(args.db)
    try:
        lifecycle.require_scenario(conn)  # checked before migrating, so a main database isn't touched
        migrate(conn)  # a scenario database made before a new migration picks it up here
        bonds = [i for i in load_instruments(Settings.from_env().instruments_file) if isinstance(i, FixedRateBond)]
        if args.command == "advance":
            before = lifecycle.advance_simulation(conn, args.date)
            print(f"simulation date {before} -> {args.date}")
        if args.command in ("pay", "advance"):
            results = lifecycle.pay_due_coupons(conn, bonds)
            for r in results:
                print(f"  {r.payment_id}: {r.status}, paid {r.total_paid:,f} of {r.total_due:,f}"
                      f" ({r.paid_now} paid by this run)" + (f"; unpaid: {', '.join(r.unpaid)}" if r.unpaid else ""))
            if not results:
                print("  no coupons due yet")
        status(conn)
    except lifecycle.LifecycleError as exc:
        sys.exit(f"refused: {exc}")
    finally:
        conn.close()


def status(conn) -> None:
    print(f"\nsimulation date {lifecycle.simulation_date(conn)}")
    for e in lifecycle.payments(conn):
        print(f"  {e['id']}  record {e['record_date']}  {e['status']:<12} "
              f"due {e['currency']} {e['total_due']}  paid {e['total_paid']}")
        for ent in e["entitlements"]:
            print(f"      {ent['holder']:<8} units {ent['units']:>10}  due {ent['amount_due']:>10}"
                  f"  {ent['status']}{'  (' + ent['reason'] + ')' if ent['reason'] else ''}")
    print("cash balances:")
    for owner, assets in sorted(ledger.balances(conn).items()):
        cash = {a: v for a, v in assets.items() if ledger.is_cash(a)}
        if cash:
            print(f"  {owner:<26} " + ", ".join(f"{a} {v:,f}" for a, v in sorted(cash.items())))
    problems = ledger.check_integrity(conn)
    print("ledger integrity:", "ok" if not problems else problems)


if __name__ == "__main__":
    main()
