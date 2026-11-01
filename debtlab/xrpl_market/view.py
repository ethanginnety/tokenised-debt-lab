"""What `GET /api/xrpl` and the dashboard show (M7 step 4). Reads only.

The monitor runs as its own process (scripts/xrpl_monitor.py) and writes its own
database. The dashboard opens that file **read-only** (SQLite `mode=ro`), so the app
can't change or create it, and reads everything in one read transaction, so a page
never mixes two moments.

LIVE MARKET DATA, READ-ONLY. Nothing here is a bond price, and nothing here trades.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from debtlab.money import D
from debtlab.xrpl_market import store

LABEL = "LIVE MARKET DATA, READ-ONLY"
NOTE = ("Real RLUSD/XRP data from the XRP Ledger's validated ledgers. Nothing here trades, and no bond "
        "value is ever used for this pair.")
HOW = "Start the monitor with: python scripts/xrpl_monitor.py run (see RUNBOOK: RLUSD/XRP monitor)."


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def xrpl_view(path: Path, now: datetime, *, trades_limit: int = 20, outages_limit: int = 20) -> dict:
    base = {"label": LABEL, "note": NOTE, "database": str(path), "generated_at": now.isoformat()}
    if not Path(path).exists():
        return {**base, "enabled": False, "reason": f"No monitor database at {path}. {HOW}"}
    try:
        conn = open_readonly(path)
    except sqlite3.Error as e:
        return {**base, "enabled": False, "reason": f"Couldn't open {path} read-only: {e}"}
    try:
        conn.execute("BEGIN")          # one read transaction: everything from the same moment
        role = conn.execute("SELECT value FROM xrpl_meta WHERE key = 'role'").fetchone()
        if not role or role["value"] != "xrpl-monitor":
            return {**base, "enabled": False, "reason": f"{path} is not a monitor database."}
        return {**base, "enabled": True, **_read(conn, now, trades_limit, outages_limit)}
    except sqlite3.Error as e:
        return {**base, "enabled": False, "reason": f"Couldn't read {path}: {e}"}
    finally:
        conn.close()


def _age(now: datetime, iso: str | None) -> float | None:
    return None if iso is None else round((now - store.parse(iso)).total_seconds(), 1)


def _snap(r: sqlite3.Row | None, now: datetime) -> dict | None:
    if r is None:
        return None
    age = _age(now, r["received_at"])
    out = {k: r[k] for k in ("ledger_index", "close_time", "received_at", "kind", "best_bid", "best_ask", "mid",
                             "spread_bp", "bid_offers", "ask_offers", "dropped", "endpoint")}
    out.update(valid=bool(r["valid"]), reasons=json.loads(r["reasons"]), age_s=age,
               fresh=age is not None and age <= store.FRESH_SECONDS,
               bids_complete=bool(r["bids_complete"]), asks_complete=bool(r["asks_complete"]),
               depth=json.loads(r["depth_json"]),
               levels=json.loads(r["levels_json"]) if r["levels_json"] else None)
    if r["amm_xrp"]:
        out["amm"] = {"xrp": r["amm_xrp"], "rlusd": r["amm_rlusd"], "fee_pct": r["amm_fee_pct"],
                      "price": str(D(r["amm_rlusd"]) / D(r["amm_xrp"]))}
    return out


def _uptime(u: store.Uptime) -> dict:
    return {"since": u.since.isoformat() if u.since else None, "hours": round(u.span_s / 3600, 2),
            "up_hours": round(u.up_s / 3600, 2), "share": u.share}


def _check(c: store.DepthCheck) -> dict:
    s = lambda x: None if x is None else str(x)  # noqa: E731
    return {"ledgers_read": c.ledgers_read, "both_sides_share": c.both_sides_share,
            "median_spread_bp": s(c.median_spread_bp), "deep_reads": c.deep_reads,
            "deep_reads_complete": c.deep_reads_complete,
            "median_bid_eur_50bp": s(c.median_bid_eur_50bp), "median_ask_eur_50bp": s(c.median_ask_eur_50bp),
            "criteria": [{"name": k, "pass": v} for k, v in c.passes.items()]}


def _read(conn: sqlite3.Connection, now: datetime, trades_limit: int, outages_limit: int) -> dict:
    run = conn.execute("SELECT * FROM xrpl_runs ORDER BY id DESC LIMIT 1").fetchone()
    hb = conn.execute("SELECT * FROM xrpl_heartbeat WHERE id = 1").fetchone()
    hb_age = _age(now, hb["at"]) if hb else None
    running = bool(run) and run["stopped_at"] is None and hb_age is not None and hb_age <= store.FRESH_SECONDS
    if not run:
        monitor = {"state": "never run", "detail": HOW}
    elif running:
        monitor = {"state": "running", "detail": f"run {run['id']} since {run['started_at']}"}
    elif run["stopped_at"]:
        monitor = {"state": "stopped", "detail": f"run {run['id']} stopped {run['stopped_at']}: {run['stop_reason']}"}
    else:
        monitor = {"state": "not running", "detail": f"no heartbeat for {hb_age:.0f}s; it stopped without a clean stop "
                                                     "(crash, sleep or power loss). The gap is recorded on its next start."}
    monitor.update(run_id=run["id"] if run else None, heartbeat_at=hb["at"] if hb else None, heartbeat_age_s=hb_age)

    latest = _snap(conn.execute("SELECT * FROM xrpl_snapshots ORDER BY ledger_index DESC LIMIT 1").fetchone(), now)
    deep = _snap(conn.execute("SELECT * FROM xrpl_snapshots WHERE kind = 'deep' AND valid = 1 "
                              "ORDER BY ledger_index DESC LIMIT 1").fetchone(), now)
    fx = store.latest_fx(conn)
    day = now - timedelta(hours=24)

    trades = [dict(r) for r in conn.execute(
        "SELECT ledger_index, venue, taker_side, price, xrp, rlusd, tx_type, tx_hash, received_at FROM xrpl_trades "
        "ORDER BY ledger_index DESC, rowid DESC LIMIT ?", (trades_limit,))]
    counts = [dict(r) for r in conn.execute(
        "SELECT venue, taker_side, COUNT(*) AS trades, SUM(CAST(xrp AS REAL)) AS xrp, SUM(CAST(rlusd AS REAL)) AS rlusd "
        "FROM xrpl_trades WHERE received_at >= ? GROUP BY venue, taker_side ORDER BY venue, taker_side", (store.iso(day),))]
    outages = []
    for r in conn.execute("SELECT * FROM xrpl_outages ORDER BY id DESC LIMIT ?", (outages_limit,)):
        end = r["ended_at"]
        dur = ((store.parse(end) if end else now) - store.parse(r["started_at"])).total_seconds()
        outages.append({"kind": r["kind"], "started_at": r["started_at"], "ended_at": end, "open": end is None,
                        "seconds": round(dur, 1), "detail": r["detail"]})
    events = [dict(r) for r in conn.execute(
        "SELECT at, kind, detail FROM xrpl_events WHERE kind <> 'connected' ORDER BY id DESC LIMIT 10")]
    issuer = conn.execute("SELECT * FROM xrpl_issuer_checks ORDER BY ledger_index DESC LIMIT 1").fetchone()
    network = conn.execute("SELECT * FROM xrpl_network ORDER BY ledger_index DESC LIMIT 1").fetchone()
    changes = conn.execute("SELECT COUNT(*) FROM xrpl_issuer_checks WHERE changed = 1").fetchone()[0]

    return {
        "monitor": monitor,
        "latest": latest,
        "latest_deep": deep,
        "fx": None if fx is None else {"usd_per_eur": str(fx.usd_per_eur), "obs_date": fx.obs_date.isoformat(),
                                       "stale": fx.stale(now), "source": fx.source,
                                       "assumption": "1 RLUSD = 1 USD (risk R19)"},
        "uptime": {"all": _uptime(store.uptime(conn, now)), "last_24h": _uptime(store.uptime(conn, now, day))},
        "decision_c": {"all": _check(store.depth_check(conn)), "last_24h": _check(store.depth_check(conn, day)),
                       "note": "Decision C, fixed before looking: both sides funded in at least 99% of ledgers; median "
                               "spread at most 50 bp; median depth within 50 bp of mid at least EUR 100 each side "
                               "(from deep reads). Judged at the checkpoint, over at least 2 days."},
        "trades": {"recent": trades, "last_24h": counts},
        "outages": outages,
        "events": events,
        "issuer": None if issuer is None else {"ledger_index": issuer["ledger_index"], "checked_at": issuer["checked_at"],
                                               "settings": json.loads(issuer["settings_json"]), "changes_seen": changes},
        "network": None if network is None else dict(network),
    }
