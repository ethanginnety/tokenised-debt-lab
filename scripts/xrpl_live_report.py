"""M8: figures for the final live report (brief §7.5), read-only on every database. REAL FUNDS, mainnet.

    python scripts/xrpl_live_report.py [--out results/m8_live_report.json]

Covers both budget periods: period 1 (session 1, 7 Nov) and period 2 (sessions 2 and 3, 8-9 Nov). Writes
the exact figures, with every fill's transaction hash, to OUT; `results/m8_live_report.md` is written from
them by hand. The P&L split walks the live journal the way the paper report walks the paper journal
(`paper/runreport.walk`): spread + fees + inventory = the change in value, with inventory split by whether
the bot was quoting, and the not-quoting part split again into paused in a session and stopped between
sessions. Uptime is the recorded rule (results/m8_live_cutoff.txt): time with a quote and no gap over 30 s,
from the paper-alongside decisions, as for paper. No tests of its own: its totals are checked against the
books (asserted below) and the books against the ledger by hash (results/m8_live_sessions.txt).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.money import D, plain  # noqa: E402
from debtlab.xrpl_market import load_mainnet_config  # noqa: E402
from debtlab.xrpl_market.live.compare import compare, double_offer_fills, fees  # noqa: E402
from debtlab.xrpl_market.paper import runreport  # noqa: E402
from debtlab.xrpl_market.paper.db import iso, parse  # noqa: E402
from debtlab.xrpl_market.paper.market import read_market  # noqa: E402

DATA = Path("C:/debtlab-data")
EPS = timedelta(microseconds=1)
# The monitor was down from 19:58 on 8 Nov (a Ctrl+C meant for the bot) to 16:50 on 9 Nov, so session 2's stop
# has no monitor mid: the order book's mid read from the ledger just after the stop instead (ledger 108204075).
BOOK_MID_S2_STOP = (parse("2026-11-08T21:17:10.851+00:00"), D("1.144093193444257789781091464"))

PERIODS = [
    {"name": "period 1", "live": "xrpl_mainnet_live.sqlite3", "paper": "xrpl_mainnet_paper.sqlite3",
     "sessions": ["session 1"]},
    {"name": "period 2", "live": "xrpl_mainnet_live_period2.sqlite3", "paper": "xrpl_mainnet_paper_period2.sqlite3",
     "sessions": ["session 2", "session 3"]},
]


def ro(name: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DATA / name}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def journal_as_paper(live: sqlite3.Connection) -> sqlite3.Connection:
    """The live journal in the paper journal's shape, so `runreport.walk` can walk it: fills are fills,
    fees and the ledger's 16-digit rounding are costs."""
    m = sqlite3.connect(":memory:")
    m.row_factory = sqlite3.Row
    m.execute("CREATE TABLE paper_journal (id INTEGER, account TEXT, at TEXT, kind TEXT, xrp TEXT, rlusd TEXT, mid TEXT)")
    for r in live.execute("SELECT * FROM live_journal ORDER BY id"):
        kind = {"maker_fill": "fill", "taker_fill": "fill", "open": "open"}.get(r["kind"], "fee")
        m.execute("INSERT INTO paper_journal VALUES (?, 'live', ?, ?, ?, ?, ?)",
                  (r["id"], r["at"], kind, str(r["xrp"]), str(r["rlusd"]), r["mid"]))
    return m


def per_paper_quote(live: sqlite3.Connection, paper: sqlite3.Connection, dbl: set[str]) -> dict:
    """Live against paper per paper order. Live follows the conservative paper account's orders; when a live
    offer fills and that paper order is still open, the bot places a new offer for the same paper order, which
    can fill again. A paper order fills at most its size, so live's fills beyond one paper order's size
    (apart from double-offer fills, counted separately) are refills paper can't have."""
    size = {r["id"]: D(r["size"]) for r in paper.execute("SELECT id, size FROM paper_orders")}
    got: dict = {}
    for f in live.execute("SELECT f.id, f.xrp, f.order_id, o.paper_order_id FROM live_fills f JOIN live_orders o ON o.id = f.order_id"):
        g = got.setdefault(f["paper_order_id"], {"xrp": Decimal(0), "orders": set(), "double": Decimal(0)})
        g["xrp"] += D(f["xrp"])
        g["orders"].add(f["order_id"])
        if f["id"] in dbl:
            g["double"] += D(f["xrp"])
    paper_fill: dict = {}
    for f in paper.execute("SELECT f.xrp, o.id AS oid, o.account FROM paper_fills f JOIN paper_orders o ON o.id = f.order_id"):
        paper_fill[(f["oid"], f["account"])] = paper_fill.get((f["oid"], f["account"]), Decimal(0)) + D(f["xrp"])
    zero = Decimal(0)
    beyond = sum((max(zero, g["xrp"] - size.get(k, zero)) for k, g in got.items()), zero)
    double = sum((g["double"] for g in got.values()), zero)
    refills = sum((max(zero, g["xrp"] - g["double"] - size.get(k, zero)) for k, g in got.items()), zero)
    return {"paper_quotes_live_filled": len(got),
            "paper_quotes_with_more_than_one_live_order_filled": sum(len(g["orders"]) > 1 for g in got.values()),
            "live_xrp": sum((g["xrp"] for g in got.values()), zero),
            "live_xrp_beyond_one_paper_order": beyond, "of_which_double_offer": beyond - refills,
            "of_which_refills": refills, "double_offer_xrp_all": double,
            "live_xrp_capped_at_one_paper_order": sum((min(g["xrp"], size.get(k, zero)) for k, g in got.items()), zero),
            "live_xrp_on_quotes_conservative_never_filled":
                sum((g["xrp"] for k, g in got.items() if paper_fill.get((k, "conservative"), zero) == 0), zero),
            "conservative_xrp_on_quotes_live_never_filled":
                sum((v for (k, a), v in paper_fill.items() if a == "conservative" and k not in got), zero)}


def split(bk: runreport.Bucket) -> dict:
    return {"spread": bk.spread, "fees": bk.fees, "inventory_quoting": bk.inv_quoting,
            "inventory_not_quoting": bk.inv_not_quoting, "total": bk.total, "fills": bk.fills,
            "bought_xrp": bk.bought_xrp, "sold_xrp": bk.sold_xrp, "fee_entries": bk.fee_count}


def add(a: runreport.Bucket, b: runreport.Bucket) -> runreport.Bucket:
    out = runreport.Bucket()
    for k in ("spread", "fees", "inv_quoting", "inv_not_quoting", "fills", "fee_count", "fees_xrp", "bought_xrp", "sold_xrp"):
        setattr(out, k, getattr(a, k) + getattr(b, k))
    return out


def period(p: dict, mon: sqlite3.Connection, cfg: dict) -> dict:
    live, paper = ro(p["live"]), ro(p["paper"])
    acct = live.execute("SELECT * FROM live_account").fetchone()
    opened = parse(acct["opened_at"])
    runs = [dict(r) for r in live.execute("SELECT * FROM live_runs ORDER BY id")]
    q = [t for t, _ in runreport.quoted_times(paper)]
    states = [dict(r) for r in live.execute("SELECT * FROM live_state_changes ORDER BY id")]

    # windows: each session from its start (the period's opening for the first) to its stop, and the gaps between
    windows, sessions = [], []
    for i, (r, name) in enumerate(zip(runs, p["sessions"])):
        a = opened if i == 0 else parse(r["started_at"])
        b = parse(r["stopped_at"])
        if i:
            windows.append((f"stopped before {name}", parse(runs[i - 1]["stopped_at"]), a))
        windows.append((name, a - EPS if i == 0 else a, b))
        qs = [t for t in q if a <= t <= b]
        sessions.append({"session": name, "run": r["id"], "started": r["started_at"], "stopped": r["stopped_at"],
                         "stop_reason": r["stop_reason"], "wall_h": (b - parse(r["started_at"])).total_seconds() / 3600,
                         "uptime_h": runreport.uptime_s(q, a, b) / 3600,
                         "first_quote": iso(qs[0]) if qs else None, "last_quote": iso(qs[-1]) if qs else None})
    end = windows[-1][2]

    marks = {}
    for t in [w[1] for w in windows] + [w[2] for w in windows]:
        m = read_market(mon, max(t, opened), cfg)
        if m.mid is not None:
            marks[max(t, opened)] = m.mid
    if p["name"] == "period 2":
        marks[BOOK_MID_S2_STOP[0]] = BOOK_MID_S2_STOP[1]
    for t, fair in runreport.quoted_times(paper):
        marks.setdefault(t, fair)
    marks = sorted((t, m) for t, m in marks.items() if t <= end)
    w = runreport.walk(journal_as_paper(live), "live", marks, windows, q, {end: ["end"]})
    per_window = {}
    for (wn, _day), bk in w.buckets.items():
        per_window[wn] = add(per_window.get(wn, runreport.Bucket()), bk)
    total = runreport.Bucket()
    for bk in per_window.values():
        total = add(total, bk)
    o, e = w.balances["open"], w.balances["end"]
    assert abs(total.total - (e[3] - o[3])) < Decimal("1e-20")
    jx = sum((D(str(r[0])) for r in live.execute("SELECT xrp FROM live_journal")), Decimal(0))
    jr = sum((D(str(r[0])) for r in live.execute("SELECT rlusd FROM live_journal")), Decimal(0))
    assert (jx, jr) == (e[0], e[1]), "the walk's end balances must be the books'"
    paused = sum((per_window[s["session"]].inv_not_quoting for s in sessions), Decimal(0))
    between = sum((bk.inv_not_quoting for wn, bk in per_window.items() if wn.startswith("stopped")), Decimal(0))
    dd, dd_from, dd_to = runreport.drawdown(w.path, opened, end)
    hold = o[0] * e[2] + o[1] - o[3]

    fills = [dict(r) for r in live.execute("SELECT * FROM live_fills ORDER BY ledger_index, id")]
    dbl = double_offer_fills(live)
    for f in fills:
        f["double_offer"] = f["id"] in dbl
    txs = Counter((r["kind"], r["result"] or r["status"]) for r in live.execute("SELECT kind, result, status FROM live_txs"))
    events = Counter(r[0] for r in live.execute("SELECT kind FROM live_events"))
    refusals = Counter()
    for (d,) in live.execute("SELECT detail FROM live_events WHERE kind = 'order_refused'"):
        d = json.loads(d)
        refusals[(d["side"], d["reasons"][0].split(":")[0])] += 1
    rows = compare(live, paper)
    cats = Counter(r.kind for r in rows)
    paper_report = runreport.build(paper, mon, cfg, cutoff_hours=1000.0)
    paper_accounts = {}
    for a in paper_report["accounts"]:
        win = a["windows"]["main"]
        paper_accounts[a["name"]] = {"rlusd": win["rlusd"], "start": win["start"], "end": win["end"],
                                     "orders": win["orders"], "drawdown_rlusd": win["drawdown_rlusd"]}

    def side(s: str, role: str | None = None) -> dict:
        fs = [f for f in fills if f["side"] == s and (role is None or f["role"] == role)]
        return {"fills": len(fs), "xrp": sum((D(f["xrp"]) for f in fs), Decimal(0)),
                "rlusd": sum((D(f["rlusd"]) for f in fs), Decimal(0))}

    return {
        "period": p["name"], "databases": {"live": p["live"], "paper": p["paper"]},
        "account": acct["address"], "opened_at": acct["opened_at"], "opened_ledger": acct["ledger_index"],
        "usd_per_eur": acct["usd_per_eur"], "set_aside": {"xrp": acct["aside_xrp"], "rlusd": acct["aside_rlusd"]},
        "end": iso(end), "sessions": sessions, "state_changes": states,
        "uptime_h": sum(s["uptime_h"] for s in sessions), "wall_h": sum(s["wall_h"] for s in sessions),
        "budget": {"open": {"xrp": o[0], "rlusd": o[1], "mid": o[2], "value_rlusd": o[3]},
                   "end": {"xrp": e[0], "rlusd": e[1], "mid": e[2], "value_rlusd": e[3]}},
        "pnl_rlusd": {**split(total), "inventory_paused_in_session": paused, "inventory_stopped_between_sessions": between},
        "pnl_by_window_rlusd": {wn: split(bk) for wn, bk in per_window.items()},
        "hold_rlusd": hold, "vs_hold_rlusd": total.total - hold,
        "drawdown_rlusd": dd, "drawdown_from": iso(dd_from) if dd_from else None, "drawdown_to": iso(dd_to) if dd_to else None,
        "fills": {"buy": side("buy"), "sell": side("sell"), "taker": [f for f in fills if f["role"] == "taker"],
                  "double_offer": {"fills": sum(f["double_offer"] for f in fills),
                                   "xrp": sum((D(f["xrp"]) for f in fills if f["double_offer"]), Decimal(0))}},
        "orders": dict(Counter(r[0] for r in live.execute("SELECT status FROM live_orders"))),
        "txs": {f"{k} {v}": n for (k, v), n in sorted(txs.items())},
        "fees_xrp": -sum((D(str(r[0])) for r in live.execute("SELECT xrp FROM live_journal WHERE kind = 'fee'")), Decimal(0)),
        "events": dict(events), "refusals": {f"{s}: {why}": n for (s, why), n in sorted(refusals.items())},
        "compare": {"categories": dict(cats),
                    "xrp": {"live": sum((r.live_maker + r.live_taker for r in rows), Decimal(0)), "live_double": sum((r.live_double for r in rows), Decimal(0)),
                            "conservative": sum((r.conservative for r in rows), Decimal(0)),
                            "optimistic": sum((r.optimistic for r in rows), Decimal(0))},
                    "fees": fees(live, paper)},
        "per_paper_quote": per_paper_quote(live, paper, dbl),
        "paper": paper_accounts,
        "fill_hashes": [{"tx": f["tx_hash"], "ledger": f["ledger_index"], "at": f["at"], "order": f["order_id"], "side": f["side"],
                         "role": f["role"], "xrp": f["xrp"], "rlusd": f["rlusd"], "double_offer": f["double_offer"]} for f in fills],
    }


def _j(x):
    if isinstance(x, Decimal):
        return plain(x)
    if isinstance(x, datetime):
        return iso(x)
    if isinstance(x, tuple):
        return list(x)
    return str(x)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "m8_live_report.json")
    a = ap.parse_args(argv)
    cfg, mon = load_mainnet_config(), ro("xrpl.sqlite3")
    out = {"label": "MAINNET: real funds", "periods": [period(p, mon, cfg) for p in PERIODS]}
    a.out.write_text(json.dumps(out, default=_j, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
