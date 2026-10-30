"""SQLite storage: migrations, the audit event log, stored curves and NAV reports.

SQLite is a single file, needs no server, and survives restarts, which is
enough while everything runs on one machine. The PostgreSQL switch happens
at the deployment milestone (M9).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from debtlab.instruments import NavReport
from debtlab.marketdata.models import CurveSnapshot

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safer under crashes and concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply numbered .sql files once each, in order. Safe to call every start."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(migrations_dir.glob("*.sql")):
        if f.name in done:
            continue
        conn.execute("BEGIN")
        try:
            _exec_script(conn, f.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (f.name, _now()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(f.name)
    return applied


def _exec_script(conn: sqlite3.Connection, sql: str) -> None:
    # executescript() would commit our open transaction, so run statements one by one.
    for stmt in (s.strip() for s in sql.split(";")):
        lines = [ln for ln in stmt.splitlines() if not ln.strip().startswith("--")]
        if "".join(lines).strip():
            conn.execute(stmt)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_event(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict,
    entity: str = "",
    mode: str = "simulation",
    idempotency_key: str | None = None,
) -> bool:
    """Append an event. Returns False if an event with the same key already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO events(created_at, kind, entity, mode, payload, idempotency_key)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), kind, entity, mode, json.dumps(payload, default=str), idempotency_key),
    )
    return cur.rowcount == 1


def recent_events(conn: sqlite3.Connection, limit: int = 50, kind: str | None = None) -> list[dict]:
    sql = "SELECT id, created_at, kind, entity, mode, payload FROM events"
    args: list = []
    if kind:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [
        {**dict(r), "payload": json.loads(r["payload"])} for r in conn.execute(sql, args)
    ]


def save_curve(conn: sqlite3.Connection, snap: CurveSnapshot) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO curve_snapshots(id, currency, kind, observation_date, fetched_at, payload)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            snap.id,
            snap.curve.currency,
            snap.kind,
            snap.observation_date.isoformat(),
            snap.fetched_at.isoformat(),
            snap.to_json(),
        ),
    )
    return cur.rowcount == 1


def latest_curve(
    conn: sqlite3.Connection, currency: str, kinds: tuple[str, ...]
) -> CurveSnapshot | None:
    marks = ",".join("?" for _ in kinds)
    row = conn.execute(
        f"SELECT payload FROM curve_snapshots WHERE currency = ? AND kind IN ({marks})"
        " ORDER BY observation_date DESC, fetched_at DESC LIMIT 1",
        (currency, *kinds),
    ).fetchone()
    return CurveSnapshot.from_json(row["payload"]) if row else None


def save_nav(conn: sqlite3.Connection, report: NavReport) -> bool:
    """Store a reported NAV. Returns False if this exact report is already stored."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO nav_reports(id, fund_id, nav_per_share, as_of, reported_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (report.id, report.fund_id, str(report.nav_per_share), report.as_of.isoformat(),
         report.reported_at.astimezone(timezone.utc).isoformat(), report.source),
    )
    return cur.rowcount == 1


def latest_nav(conn: sqlite3.Connection, fund_id: str) -> NavReport | None:
    """The NAV for the most recent as-of date; a later restatement of it wins."""
    row = conn.execute(
        "SELECT * FROM nav_reports WHERE fund_id = ? ORDER BY as_of DESC, reported_at DESC LIMIT 1",
        (fund_id,),
    ).fetchone()
    return NavReport.from_dict(fund_id, dict(row)) if row else None
