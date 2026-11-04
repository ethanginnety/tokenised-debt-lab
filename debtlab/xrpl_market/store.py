"""The monitor's database: snapshots, trades, outages, heartbeat, and what `status` reports.

Its own SQLite file, migrated from migrations/xrpl/, never a bond database. Every write
takes the time as an argument, so tests control the clock.

Uptime is worked out from the data itself, not from what the monitor says about itself:
the share of the time since monitoring started that was covered by snapshots no more than
`FRESH_SECONDS` apart (and the latest no older than that). Time the monitor wasn't running,
was disconnected or got no validated ledger counts as down, whatever caused it. The
outages table says *why*.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median

from debtlab.audit.db import connect, migrate
from debtlab.money import D
from debtlab.xrpl_market import book
from debtlab.xrpl_market.fx import PAIR, FxRate
from debtlab.xrpl_market.trades import Trade
from debtlab.xrpl_market.verify import IssuerSettings, NetworkCosts

MIGRATIONS = Path(__file__).resolve().parent.parent.parent / "migrations" / "xrpl"
RIPPLE_EPOCH = 946_684_800    # 2000-01-01 UTC, in Unix seconds
FRESH_SECONDS = 30            # the brief's live rule: data older than 30 s is stale
LEVELS_KEPT = 10              # top levels stored per side
THIN_AFTER = timedelta(days=3)


class WrongDatabase(Exception):
    pass


def iso(t: datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("times must be timezone-aware (UTC)")
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def network(conn: sqlite3.Connection) -> str | None:
    """Which network this database records. One with data but no label predates testnet (M8): mainnet."""
    row = conn.execute("SELECT value FROM xrpl_meta WHERE key = 'network'").fetchone()
    if row:
        return row["value"]
    return "mainnet" if conn.execute("SELECT 1 FROM xrpl_snapshots LIMIT 1").fetchone() else None


def claim_network(conn: sqlite3.Connection, name: str) -> None:
    """Label a database with its network, or refuse if it already records another one, so
    testnet data can never land in the mainnet evidence, or the other way round."""
    have = network(conn)
    if have is not None and have != name:
        raise WrongDatabase(f"this database records {have}, not {name}; use a separate database for each network")
    conn.execute("INSERT OR IGNORE INTO xrpl_meta(key, value) VALUES ('network', ?)", (name,))
    conn.commit()


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open (and create or migrate) the monitor's database. Refuses a bond database."""
    conn = connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables & {"ledger_txns", "curve_snapshots", "events"}:
        conn.close()
        raise WrongDatabase(f"{path} is a bond database; the monitor uses its own (e.g. data/xrpl.sqlite3)")
    migrate(conn, MIGRATIONS)
    return conn


# --- runs, heartbeat, outages, events --------------------------------------------------

def start_run(conn: sqlite3.Connection, now: datetime) -> int:
    """Start a run. If the monitor stopped (cleanly or not) more than FRESH_SECONDS ago,
    the time since its last heartbeat is recorded as *not running*."""
    with _tx(conn):
        hb = conn.execute("SELECT at FROM xrpl_heartbeat WHERE id = 1").fetchone()
        last = parse(hb["at"]) if hb else None
        if last is not None:
            _close_open(conn, last)                 # whatever was open ends when we went quiet
            # The last sign of life: the heartbeat, or the end of an outage recorded after it,
            # so outages never overlap.
            ended = conn.execute("SELECT MAX(ended_at) AS t FROM xrpl_outages").fetchone()["t"]
            gap_from = max(last, parse(ended)) if ended else last
            conn.execute("UPDATE xrpl_runs SET stopped_at = ?, "
                         "stop_reason = 'no clean stop (crash, sleep or power loss)' "
                         "WHERE stopped_at IS NULL", (iso(gap_from),))
            if (now - gap_from).total_seconds() > FRESH_SECONDS:
                conn.execute("INSERT INTO xrpl_outages(kind, started_at, ended_at, detail) VALUES (?,?,?,?)",
                             ("not_running", iso(gap_from), iso(now), "monitor not running (gap since last heartbeat)"))
        run = conn.execute("INSERT INTO xrpl_runs(started_at) VALUES (?)", (iso(now),)).lastrowid
        _heartbeat(conn, run, now, None)
    return run


def stop_run(conn: sqlite3.Connection, run_id: int, now: datetime, reason: str) -> None:
    with _tx(conn):
        _heartbeat(conn, run_id, now, None)
        conn.execute("UPDATE xrpl_runs SET stopped_at = ?, stop_reason = ? WHERE id = ?", (iso(now), reason, run_id))


def heartbeat(conn: sqlite3.Connection, run_id: int, now: datetime, ledger_index: int | None = None) -> None:
    with _tx(conn):
        _heartbeat(conn, run_id, now, ledger_index)


def _heartbeat(conn, run_id, now, ledger_index):
    conn.execute("INSERT INTO xrpl_heartbeat(id, at, run_id, ledger_index) VALUES (1, ?, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET at = excluded.at, run_id = excluded.run_id, "
                 "ledger_index = COALESCE(excluded.ledger_index, xrpl_heartbeat.ledger_index)",
                 (iso(now), run_id, ledger_index))


def open_outage(conn: sqlite3.Connection, kind: str, at: datetime, detail: str) -> None:
    """Open an outage unless one of the same kind is already open; a different open one
    is closed at the same moment, so at most one is ever open."""
    with _tx(conn):
        cur = conn.execute("SELECT kind FROM xrpl_outages WHERE ended_at IS NULL").fetchone()
        if cur and cur["kind"] == kind:
            return
        _close_open(conn, at)
        conn.execute("INSERT INTO xrpl_outages(kind, started_at, detail) VALUES (?,?,?)", (kind, iso(at), detail))


def close_outages(conn: sqlite3.Connection, at: datetime) -> None:
    with _tx(conn):
        _close_open(conn, at)


def _close_open(conn, at):
    conn.execute("UPDATE xrpl_outages SET ended_at = MAX(started_at, ?) WHERE ended_at IS NULL", (iso(at),))


def event(conn: sqlite3.Connection, at: datetime, kind: str, detail: str) -> None:
    with _tx(conn):
        conn.execute("INSERT INTO xrpl_events(at, kind, detail) VALUES (?,?,?)", (iso(at), kind, detail))


# --- snapshots and trades --------------------------------------------------------------

def record_snapshot(conn: sqlite3.Connection, run_id: int, endpoint: str, ledger_index: int, close_time: datetime,
                    received_at: datetime, kind: str, b: book.Book, pool, exclude: str | None = None) -> book.Stats:
    """Store the book at one ledger, close any outage, and record a gap if ledgers were missed."""
    st = book.stats(b)
    ref = book.reference(b, pool, int(close_time.timestamp()) - RIPPLE_EPOCH, exclude)
    levels = {"bids": [[str(lv.price), str(lv.xrp)] for lv in b.bids[:LEVELS_KEPT]],
              "asks": [[str(lv.price), str(lv.xrp)] for lv in b.asks[:LEVELS_KEPT]]}
    depth = [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(d).items()} for d in st.depth]
    s = lambda x: None if x is None else str(x)  # noqa: E731
    with _tx(conn):
        prev = conn.execute("SELECT ledger_index, received_at FROM xrpl_snapshots WHERE run_id = ? "
                            "ORDER BY ledger_index DESC LIMIT 1", (run_id,)).fetchone()
        if prev and ledger_index > prev["ledger_index"] + 1:
            missed = ledger_index - prev["ledger_index"] - 1
            note = f"{missed} ledger(s) not read: {prev['ledger_index'] + 1}..{ledger_index - 1}"
            open_ = conn.execute("SELECT id FROM xrpl_outages WHERE ended_at IS NULL").fetchone()
            if open_:   # a disconnection or stale spell already explains the gap: note it there
                conn.execute("UPDATE xrpl_outages SET detail = detail || '; ' || ? WHERE id = ?", (note, open_["id"]))
            else:
                conn.execute("INSERT INTO xrpl_outages(kind, started_at, ended_at, detail) VALUES (?,?,?,?)",
                             ("missed_ledgers", prev["received_at"], iso(received_at), note))
        conn.execute(
            "INSERT OR IGNORE INTO xrpl_snapshots(ledger_index, run_id, endpoint, close_time, received_at, kind, valid,"
            " reasons, best_bid, best_ask, mid, spread_bp, bid_offers, ask_offers, dropped, bids_complete,"
            " asks_complete, depth_json, levels_json, amm_xrp, amm_rlusd, amm_fee_pct, ref_bid, ref_ask, ref_mid,"
            " ref_reasons, excluded_account, own_left_out, expired_left_out)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ledger_index, run_id, endpoint, iso(close_time), iso(received_at), kind, int(st.valid),
             json.dumps(list(st.reasons)), s(st.best_bid), s(st.best_ask), s(st.mid), s(st.spread_bp),
             len(b.bids), len(b.asks), b.dropped, int(b.bids_complete), int(b.asks_complete),
             json.dumps(depth), json.dumps(levels),
             s(pool.xrp if pool else None), s(pool.rlusd if pool else None), s(pool.trading_fee_pct if pool else None),
             s(ref.bid), s(ref.ask), s(ref.mid), json.dumps(list(ref.reasons)), exclude, ref.own_left_out, ref.expired_left_out))
        _close_open(conn, received_at)
        _heartbeat(conn, run_id, received_at, ledger_index)
    return st


def record_trades(conn: sqlite3.Connection, trades: list[Trade], received_at: datetime) -> int:
    """Insert trades; the same (transaction, node) seen twice (two subscriptions) is kept once."""
    n = 0
    with _tx(conn):
        for t in trades:
            s = lambda x: None if x is None else str(x)  # noqa: E731
            n += conn.execute(
                "INSERT OR IGNORE INTO xrpl_trades(tx_hash, node, ledger_index, venue, taker_side, price, xrp, rlusd,"
                " maker, pool_price_before, pool_price_after, tx_type, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.tx_hash, t.node, t.ledger_index, t.venue, t.taker_side, str(t.price), str(t.xrp), str(t.rlusd),
                 t.maker, s(t.pool_price_before), s(t.pool_price_after), t.tx_type, iso(received_at))).rowcount
    return n


def record_issuer(conn: sqlite3.Connection, s: IssuerSettings, now: datetime) -> bool:
    """Store the issuer's settings; True (and an event) if they changed since the last check."""
    keep = {"domain": s.domain, "flags": s.flags, "transfer_fee_pct": str(s.transfer_fee_pct), "tick_size": s.tick_size}
    with _tx(conn):
        last = conn.execute("SELECT settings_json FROM xrpl_issuer_checks ORDER BY ledger_index DESC LIMIT 1").fetchone()
        changed = bool(last) and json.loads(last["settings_json"]) != keep
        conn.execute("INSERT OR IGNORE INTO xrpl_issuer_checks(ledger_index, checked_at, settings_json, changed) "
                     "VALUES (?,?,?,?)", (s.ledger_index, iso(now), json.dumps(keep, sort_keys=True), int(changed)))
        if changed:
            conn.execute("INSERT INTO xrpl_events(at, kind, detail) VALUES (?,?,?)",
                         (iso(now), "issuer_changed", f"RLUSD issuer settings changed at ledger {s.ledger_index}: {keep}"))
    return changed


def record_network(conn: sqlite3.Connection, n: NetworkCosts, now: datetime) -> None:
    with _tx(conn):
        conn.execute("INSERT OR IGNORE INTO xrpl_network VALUES (?,?,?,?,?,?)",
                     (n.ledger_index, iso(now), str(n.reserve_base_xrp), str(n.reserve_inc_xrp),
                      str(n.base_fee_drops), str(n.current_fee_drops)))


def record_fx(conn: sqlite3.Connection, fx: FxRate, now: datetime) -> None:
    with _tx(conn):
        conn.execute("INSERT OR IGNORE INTO fx_rates(pair, obs_date, rate, source, fetched_at) VALUES (?,?,?,?,?)",
                     (PAIR, fx.obs_date.isoformat(), str(fx.usd_per_eur), fx.source, iso(now)))


def latest_fx(conn: sqlite3.Connection) -> FxRate | None:
    from datetime import date
    r = conn.execute("SELECT obs_date, rate, source FROM fx_rates WHERE pair = ? ORDER BY obs_date DESC LIMIT 1",
                     (PAIR,)).fetchone()
    return FxRate(date.fromisoformat(r["obs_date"]), D(r["rate"]), r["source"]) if r else None


def thin(conn: sqlite3.Connection, now: datetime) -> int:
    """Drop stored levels older than THIN_AFTER; the summary of every snapshot is kept."""
    with _tx(conn):
        return conn.execute("UPDATE xrpl_snapshots SET levels_json = NULL WHERE levels_json IS NOT NULL "
                            "AND received_at < ?", (iso(now - THIN_AFTER),)).rowcount


# --- what status reports ---------------------------------------------------------------

@dataclass(frozen=True)
class Uptime:
    since: datetime | None
    span_s: float
    up_s: float

    @property
    def share(self) -> float | None:
        return self.up_s / self.span_s if self.span_s > 0 else None


def uptime(conn: sqlite3.Connection, now: datetime, since: datetime | None = None) -> Uptime:
    """Share of the time since `since` (default: the first run's start) with fresh data."""
    first = conn.execute("SELECT MIN(started_at) AS t FROM xrpl_runs").fetchone()["t"]
    if first is None:
        return Uptime(None, 0.0, 0.0)
    start = max(parse(first), since) if since else parse(first)
    times = [parse(r["received_at"]) for r in conn.execute(
        "SELECT received_at FROM xrpl_snapshots WHERE received_at >= ? ORDER BY received_at", (iso(start),))]
    up = 0.0
    for a, b in zip(times, times[1:]):
        gap = (b - a).total_seconds()
        if gap <= FRESH_SECONDS:
            up += gap
    if times:
        tail = (now - times[-1]).total_seconds()
        up += min(tail, FRESH_SECONDS) if tail >= 0 else 0
    return Uptime(start, max((now - start).total_seconds(), 0.0), up)


@dataclass(frozen=True)
class DepthCheck:
    """Decision C, over a window: both sides funded in >= 99% of ledgers read; median spread
    <= 50 bp; median depth within 50 bp of mid >= EUR 100 each side (deep reads only)."""
    ledgers_read: int
    both_sides_share: float | None
    median_spread_bp: Decimal | None
    deep_reads: int
    deep_reads_complete: int
    median_bid_eur_50bp: Decimal | None
    median_ask_eur_50bp: Decimal | None
    fx: FxRate | None
    passes: dict[str, bool | None]


def depth_check(conn: sqlite3.Connection, since: datetime | None = None, until: datetime | None = None) -> DepthCheck:
    q = "SELECT * FROM xrpl_snapshots WHERE 1=1"
    args: list = []
    if since:
        q += " AND received_at >= ?"
        args.append(iso(since))
    if until:
        q += " AND received_at <= ?"
        args.append(iso(until))
    rows = conn.execute(q, args).fetchall()
    fx = latest_fx(conn)
    both = sum(1 for r in rows if r["bid_offers"] > 0 and r["ask_offers"] > 0)
    spreads = [D(r["spread_bp"]) for r in rows if r["valid"]]
    deep = [r for r in rows if r["kind"] == "deep" and r["valid"]]
    bid50, ask50, complete = [], [], 0
    for r in deep:
        d = {x["band_bp"]: x for x in json.loads(r["depth_json"])}.get(50)
        if d:
            complete += int(d["complete"])
            bid50.append(D(d["bid_rlusd"]))
            ask50.append(D(d["ask_rlusd"]))
    to_eur = (lambda x: x / fx.usd_per_eur) if fx else None
    mb = to_eur(median(bid50)) if bid50 and to_eur else None
    ma = to_eur(median(ask50)) if ask50 and to_eur else None
    share = both / len(rows) if rows else None
    ms = median(spreads) if spreads else None
    passes = {
        "both sides funded in >= 99% of ledgers": None if share is None else share >= 0.99,
        "median spread <= 50 bp": None if ms is None else ms <= 50,
        "median depth within 50 bp >= EUR 100 each side": None if mb is None or ma is None else (mb >= 100 and ma >= 100),
    }
    return DepthCheck(len(rows), share, ms, len(deep), complete, mb, ma, fx, passes)


# --- transactions ----------------------------------------------------------------------

class _tx:
    """BEGIN IMMEDIATE ... COMMIT, or join a transaction already open."""

    def __init__(self, conn):
        self.conn, self.own = conn, False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self.own = True
        return self.conn

    def __exit__(self, exc_type, *_):
        if self.own:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
