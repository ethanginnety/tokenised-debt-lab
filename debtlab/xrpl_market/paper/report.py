"""What the paper bot reports: state, limits, both accounts side by side. PAPER.

Both fill rules are shown side by side; the truth is somewhere between them (decision E).
Values are in RLUSD at the latest mid, and in euro at the latest ECB rate (1 RLUSD = 1 USD).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from debtlab.money import plain
from debtlab.xrpl_market.paper import LABEL, account
from debtlab.xrpl_market.paper.db import limits, parse, state
from debtlab.xrpl_market.paper.market import Market

NOTE = ("PAPER: no order was placed on any ledger. Fills are inferred from real trades under two rules; "
        "the truth is somewhere between the conservative and optimistic accounts.")


def _p(x):
    return None if x is None else plain(x) if isinstance(x, Decimal) else x


def status(conn: sqlite3.Connection, mon: sqlite3.Connection, m: Market, now: datetime, cfg: dict) -> dict:
    run = conn.execute("SELECT * FROM paper_runs ORDER BY id DESC LIMIT 1").fetchone()
    hb = conn.execute("SELECT at FROM paper_heartbeat WHERE id = 1").fetchone()
    out = {"label": LABEL, "note": NOTE, "state": state(conn), "limits": {k: plain(v) for k, v in limits(conn).items()},
           "run": dict(run) if run else None,
           "heartbeat_age_s": None if hb is None else round((now - parse(hb["at"])).total_seconds(), 1),
           "market": {"ledger_index": m.ledger_index, "mid": _p(m.mid), "age_s": m.age_s, "problems": m.problems(
               limits(conn).get("max_data_age_seconds", Decimal(30))),
               "usd_per_eur": None if m.fx is None else plain(m.fx.usd_per_eur)},
           "accounts": []}
    for a in account.accounts(conn):
        name = a["name"]
        xrp, rlusd = account.balances(conn, name)
        opened = account.open_orders(conn, name)
        acct = {"name": name, "rule": a["rule"], "opened_at": a["opened_at"], "xrp": plain(xrp), "rlusd": plain(rlusd),
                "reserve_xrp": plain(account.reserve(m, cfg, len(opened))),
                "open_orders": [{k: _p(o[k]) for k in ("id", "side", "price", "size", "filled", "placed_at")}
                                for o in opened]}
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM paper_orders WHERE account = ? GROUP BY status", (name,))}
        acct["orders"] = {"placed": sum(counts.values()), **counts}
        if m.mid is not None:
            p = account.pnl(conn, name, m.mid)
            acct["pnl_rlusd"] = p.as_dict()
            if m.fx is not None:
                acct["pnl_eur"] = {k: plain(m.to_eur(getattr(p, k))) for k in ("start_value", "value", "total",
                                                                                 "spread", "fees", "inventory")}
                day = account.daily_pnl_eur(conn, name, m, now)
                acct["today_eur"] = None if day is None else plain(day)
        acct["markouts_rlusd"] = {str(h): {**v, "rlusd": plain(v["rlusd"])} for h, v in account.markouts(
            conn, mon, name, now, cfg["paper"]["markout_seconds"]).items()}
        out["accounts"].append(acct)
    d = conn.execute("SELECT * FROM paper_decisions ORDER BY id DESC LIMIT 1").fetchone()
    out["last_decision"] = None if d is None else {**dict(d), "reasons": json.loads(d["reasons"]),
                                                   "components": json.loads(d["components"])}
    out["refusals"] = conn.execute("SELECT COUNT(*) FROM paper_events WHERE kind = 'order_refused'").fetchone()[0]
    out["events"] = [dict(r) for r in conn.execute(
        "SELECT at, kind, detail FROM paper_events WHERE kind NOT IN ('order_placed', 'order_cancelled')"
        " ORDER BY id DESC LIMIT 10")]
    return out


def _r(x, places: int = 4) -> str:
    """For display only: stored and reported values stay exact."""
    return "-" if x is None else f"{Decimal(str(x)):,.{places}f}"


def render(s: dict) -> str:
    lines = [f"=== {s['label']} === {s['note']}",
             f"State: {s['state']['state']} ({s['state']['reason']})",
             "Limits: " + ", ".join(f"{k} {v}" for k, v in s["limits"].items())]
    mk = s["market"]
    lines.append(f"Market: ledger {mk['ledger_index']}, mid {_r(mk['mid'], 6)}, data age {mk['age_s']}s"
                 + (f"; NOT USABLE: {'; '.join(mk['problems'])}" if mk["problems"] else ""))
    if s["run"]:
        r = s["run"]
        lines.append(f"Run {r['id']} started {r['started_at']}" + (f", stopped {r['stopped_at']} ({r['stop_reason']})"
                                                                    if r["stopped_at"] else "")
                     + f"; heartbeat {s['heartbeat_age_s']}s ago")
    if not s["accounts"]:
        lines.append("No paper accounts yet: they open at the first tick while running.")
    for a in s["accounts"]:
        lines.append(f"--- {a['name']} rule: {_r(a['xrp'], 6)} XRP (reserve {a['reserve_xrp']}), {_r(a['rlusd'], 6)} RLUSD;"
                     f" orders {a['orders']}")
        if "pnl_eur" in a:
            e = a["pnl_eur"]
            lines.append(f"    P&L EUR: total {_r(e['total'])} = spread {_r(e['spread'])} + inventory {_r(e['inventory'])}"
                         f" + fees {_r(e['fees'])}; today {_r(a.get('today_eur'))}; value {_r(e['value'])}"
                         f" (start {_r(e['start_value'])})")
        lines.append("    markouts RLUSD: " + ", ".join(f"{h}s {_r(v['rlusd'])} over {v['fills']} fills"
                                                        for h, v in a["markouts_rlusd"].items()))
        for o in a["open_orders"]:
            lines.append(f"    open: {o['side']} {o['size']} XRP at {o['price']} (filled {o['filled']})")
    if s["last_decision"]:
        d = s["last_decision"]
        lines.append(f"Last decision (ledger {d['ledger_index']}): " + (
            f"bid {d['bid_price']} / ask {d['ask_price']}, size {d['bid_size']}" if d["quoted"]
            else "no quote: " + "; ".join(d["reasons"])))
    lines.append(f"Refusals logged: {s['refusals']}")
    for e in s["events"]:
        lines.append(f"  {e['at']} {e['kind']}: {e['detail'][:160]}")
    return "\n".join(lines)
