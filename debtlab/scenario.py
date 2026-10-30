"""The scenario database: a throwaway copy for lifecycle demos.

Lifecycle scenarios (advancing the simulation date, triggering a default) run
only in a database marked 'scenario', so the main database keeps a performing
short bond for the market maker. Creating a scenario database again starts
from scratch; the previous one is moved to data/archive/, never deleted.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from debtlab import ledger, lifecycle
from debtlab.audit.db import connect, get_db_role, migrate, record_event, set_db_role
from debtlab.config import ROOT, Settings
from debtlab.instruments import load_instruments
from debtlab.marketmaker.book import load_book_config, seed_book

DEFAULT_SCENARIO_DB = ROOT / "data" / "scenario.sqlite3"
SQLITE_SIDE_FILES = ("-wal", "-shm")  # WAL-mode companions that belong with the main file


def create_scenario_db(path: Path, settings: Settings, now: datetime | None = None,
                       market_maker: bool = False) -> Path | None:
    """Create a fresh, seeded scenario database at `path`, with the market maker's book if asked.

    Without it the test holders are the only holders of record, so lifecycle figures
    are those of M3; with it the market maker and the simulated market hold units too
    and are paid like any holder.

    If one already exists it is archived first; returns the archive path, or None.
    Refuses to touch any existing database not marked 'scenario'.
    """
    now = now or datetime.now(timezone.utc)
    path = Path(path)
    archived = _archive(path, now) if path.exists() else None

    conn = connect(path)
    try:
        migrate(conn)
        set_db_role(conn, "scenario")
        instruments = load_instruments(settings.instruments_file)
        posted = ledger.seed(conn, ledger.load_holders_config(settings.holders_file), instruments)
        if market_maker:
            posted += seed_book(conn, load_book_config(settings.marketmaker_file), instruments)
        lifecycle.start_simulation(conn, now.date())  # the simulation starts today and only moves forward
        record_event(conn, "scenario_created", {
            "path": str(path), "seed_transactions": posted, "market_maker": market_maker,
            "archived_previous": str(archived) if archived else None,
        }, idempotency_key="scenario_created")
    finally:
        conn.close()
    return archived


def _archive(path: Path, now: datetime) -> Path:
    conn = connect(path)
    try:  # read the role without changing the file: a database with no role table isn't a scenario
        role = get_db_role(conn)
    finally:
        conn.close()
    if role != "scenario":
        raise ValueError(f"{path} is not a scenario database (role: {role}); refusing to replace it")
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{path.stem}-{now.strftime('%Y%m%dT%H%M%SZ')}{path.suffix}"
    try:
        for suffix in ("", *SQLITE_SIDE_FILES):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.move(src, Path(str(target) + suffix))
    except PermissionError as exc:  # Windows: the app still has it open
        raise RuntimeError(f"can't move {path}: stop the app that is using it, then try again") from exc
    return target
