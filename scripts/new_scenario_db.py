"""Create a fresh scenario database with the seeded test holders.

    python scripts/new_scenario_db.py [path]      (default: data/scenario.sqlite3)

Run it again any time to start the lifecycle demo from scratch: the previous
scenario database is moved to data/archive/, never deleted. It refuses to
replace a database that isn't marked as a scenario (such as the main one).

Then point the app at it:
    PowerShell:  $env:DEBTLAB_DB_PATH = "data/scenario.sqlite3"; python -m debtlab.api
    bash:        DEBTLAB_DB_PATH=data/scenario.sqlite3 python -m debtlab.api
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debtlab import ledger  # noqa: E402
from debtlab.audit.db import connect  # noqa: E402
from debtlab.config import Settings  # noqa: E402
from debtlab.scenario import DEFAULT_SCENARIO_DB, create_scenario_db  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SCENARIO_DB
    settings = Settings.from_env()
    if path.resolve() == settings.db_path.resolve():
        sys.exit(f"{path} is the app's main database (DEBTLAB_DB_PATH); choose another path")
    try:
        archived = create_scenario_db(path, settings)
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"not created: {exc}")
    if archived:
        print(f"previous scenario database archived to {archived}")
    conn = connect(path)
    holders = [p for p in ledger.parties(conn) if p["role"] == "holder"]
    print(f"created scenario database {path}: {len(holders)} test holders")
    for p in holders:
        held = ledger.balances(conn).get(p["id"], {})
        positions = ", ".join(f"{a} {v:,f}" for a, v in sorted(held.items()))
        print(f"  {p['name']:<28} {positions}{'  [payments blocked]' if p['payments_blocked'] else ''}")
    problems = ledger.check_integrity(conn)
    print("ledger integrity:", "ok" if not problems else problems)
    conn.close()


if __name__ == "__main__":
    main()
