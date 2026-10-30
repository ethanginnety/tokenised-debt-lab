"""Run the bond lifecycle in the scenario database.

    python scripts/lifecycle.py status                  simulation date, bond status and price, payments, amounts owed
    python scripts/lifecycle.py pay                     make every payment due by the simulation date
    python scripts/lifecycle.py advance 2027-02-17      move the simulation date forward, then pay what is due
    python scripts/lifecycle.py default SHORT-USD-2029  trigger the default scenario: the bond's next coupon is missed

Add --db PATH to use another scenario database (default: data/scenario.sqlite3).
It refuses to run on a database that isn't marked as a scenario.
Running `pay` again is always safe: only amounts still due are paid.

Prices use the latest official curve stored in the scenario database (stored when
the app has run on it), held constant at the simulation date; if there is none,
the illustrative curve, labelled as such.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debtlab import ledger, lifecycle  # noqa: E402
from debtlab.audit.db import connect, latest_curve, migrate  # noqa: E402
from debtlab.config import Settings  # noqa: E402
from debtlab.instruments import load_instruments  # noqa: E402
from debtlab.marketdata.adapters import illustrative_curves, to_zero_snapshot  # noqa: E402
from debtlab.marketdata.models import MARKET_KINDS  # noqa: E402
from debtlab.money import D  # noqa: E402
from debtlab.pricing.bond import FixedRateBond  # noqa: E402
from debtlab.scenario import DEFAULT_SCENARIO_DB  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "pay", "advance", "default"))
    parser.add_argument("target", nargs="?", help="new simulation date (advance) or bond id (default)")
    parser.add_argument("--db", type=Path, default=DEFAULT_SCENARIO_DB)
    args = parser.parse_args()
    if args.command in ("advance", "default") and args.target is None:
        parser.error("advance needs a date (advance 2027-02-17); default needs a bond (default SHORT-USD-2029)")
    if not args.db.exists():
        sys.exit(f"{args.db} doesn't exist; create it with: python scripts/new_scenario_db.py")

    conn = connect(args.db)
    try:
        lifecycle.require_scenario(conn)  # checked before migrating, so a main database isn't touched
        migrate(conn)  # a scenario database made before a new migration picks it up here
        bonds = [i for i in load_instruments(Settings.from_env().instruments_file) if isinstance(i, FixedRateBond)]
        if args.command == "default":
            bond = next((b for b in bonds if b.id == args.target), None)
            if bond is None:
                sys.exit(f"unknown bond {args.target}; one of: {', '.join(b.id for b in bonds)}")
            missed = lifecycle.trigger_default(conn, bond)
            print(f"default scenario triggered for {bond.id}: {missed.id} will be missed "
                  f"(advance to {missed.pay_date} or later to see it)")
        if args.command == "advance":
            before = lifecycle.advance_simulation(conn, date.fromisoformat(args.target))
            print(f"simulation date {before} -> {args.target}")
        if args.command in ("pay", "advance"):
            results = lifecycle.pay_due(conn, bonds)
            for r in results:
                print(f"  {r.payment_id}: {r.status}, paid {r.total_paid:,f} of {r.total_due:,f}"
                      f" ({r.paid_now} paid by this run)" + (f"; unpaid: {', '.join(r.unpaid)}" if r.unpaid else ""))
            if not results:
                print("  no payments due yet")
        status(conn, bonds)
    except lifecycle.LifecycleError as exc:
        sys.exit(f"refused: {exc}")
    finally:
        conn.close()


def curve_for(conn, currency: str):
    """(curve, label): the latest stored official curve, else the illustrative one."""
    snap = latest_curve(conn, currency, MARKET_KINDS)
    if snap is not None:
        snap = to_zero_snapshot(snap)
        return snap.curve, f"{snap.source} observed {snap.observation_date}, held constant"
    snap = illustrative_curves(datetime.now(timezone.utc))[currency]
    return snap.curve, "ILLUSTRATIVE curve, not market data"


def status(conn, bonds) -> None:
    today = lifecycle.simulation_date(conn)
    print(f"\nsimulation date {today}")
    for b in bonds:
        st = lifecycle.instrument_status(conn, b.id)
        outstanding = -ledger.balance(conn, ledger.issuer_id(b.id), b.id)
        print(f"  {b.id:<18} {st['status']:<12}{' since ' + st['since'] if st['since'] else ''}"
              f"  units outstanding {outstanding:,f}{'  (' + st['reason'] + ')' if st['reason'] else ''}")
        curve, label = curve_for(conn, b.benchmark)
        p = lifecycle.scenario_price(conn, b, curve, today)
        if p["basis"] == "none":
            print(f"      price: none ({p['message']})")
        else:
            print(f"      price {p['clean']} clean, {p['dirty']} dirty{' (trades flat)' if p.get('flat') else ''}"
                  f"; quoting {p['quoting']}; {p['basis']}, spread {p['spread_bp']} bp; {label}")
        d = lifecycle.default_terms(conn, b.id)
        if d is not None:
            declared, recovery = lifecycle.default_dates(d)
            print(f"      default: triggered {d['triggered_on']}, missed {d['missed_payment_id']}, "
                  f"declared {declared} (after {d['grace_days']} days), recovery {d['recovery_rate'] * 100:f}% "
                  f"of claim on {recovery}")
            for x in lifecycle.default_losses(conn, b):
                print(f"      {x['holder']:<8} claim {D(x['claim']):>14,f}  recovery {D(x['recovery_due']):>14,f}"
                      f" ({x['status']})  loss {D(x['loss']):>14,f} ({x['loss_pct']}%)")
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
    owed: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for u in lifecycle.unpaid(conn):
        owed[(u["holder"], u["currency"])] += D(u["amount_due"])
    if owed:
        print("owed and unpaid:")
        for (holder, ccy), amount in sorted(owed.items()):
            print(f"  {holder:<26} {ccy} {amount:,f}")
    problems = ledger.check_integrity(conn)
    print("ledger integrity:", "ok" if not problems else problems)


if __name__ == "__main__":
    main()
