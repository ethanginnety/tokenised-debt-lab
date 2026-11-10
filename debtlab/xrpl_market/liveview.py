"""What `GET /api/live` and the dashboard's *Live trading* section show: the M8 mainnet run, after it ended.

MAINNET: REAL FUNDS. Reads only. The exact figures come from the final report's JSON
(`results/m8_live_report.json`, written by scripts/xrpl_live_report.py and checked against the ledger by hash);
the timeline, the P&L path and the fills come from the live and paper-alongside databases of both budget
periods, each opened **read-only** (SQLite `mode=ro`) in one read transaction. The databases the report names
are the ones opened, so the two can't drift apart unseen: every fill hash in the databases is checked against
the report's, and the quoting time is recounted from the decisions.

Nothing here signs, sends or trades, and nothing starts a bot.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from debtlab.money import D, plain
from debtlab.xrpl_market.live.compare import double_offer_fills
from debtlab.xrpl_market.store import iso, parse
from debtlab.xrpl_market.view import open_readonly

LABEL = "MAINNET: real funds, EUR 50 experiment, run ended"
EXPLORER = "https://livenet.xrpl.org/transactions/"
GAP_S = 30            # the uptime rule: time quoting, with no gap over 30 s
POINT_S = 60          # the P&L path keeps one point a minute
BREAK_S = 120         # within a session, a longer gap between marks is drawn as "no mark" (connection or monitor down)
ZERO = Decimal(0)

UPTIME_RULE = ('"up to 24 h of uptime, summed across supervised sessions (uptime as for paper: time quoting, '
               'with no gap over 30 s), OR the hard stop below, whichever comes first." Hard stop: '
               '"Wednesday 11 Nov 2026, 20:00 Irish time = 2026-11-11T20:00:00Z". Recorded 7 Nov 2026, '
               'before any live order (results/m8_live_cutoff.txt).')
RULES = {
    "r21": ("Session 1, 7 Nov: the bot sized a replacement offer as if the offer it replaced were still whole, "
            "while someone was filling that old offer. Both filled, so one quote's worth traded twice. It happened "
            "35 times, both filled in 15, and the last took the period 1 XRP budget to -1.881683, out of the XRP "
            "set aside: about EUR 2.20 more than the bot was allowed to use. Nothing paused. The ledger reserve held "
            "and the books matched the ledger."),
    "r21_fix": ("Fixed before session 2, approved by Ethan, with a test that fails on the session 1 code: an offer "
                "being replaced counts in full against the budget and the EUR 50 total until its replacement "
                "settles, and a budget below zero on either side pauses the bot. In period 2 the budget never went "
                "below zero. The race itself still happens; it no longer breaks the budget."),
    "taker": ("The bot only means to rest offers on the book (maker). An XRPL offer can still take liquidity if the "
              "price moves in the 2 ledgers before it lands, and tfPassive only stops crossing at an equal price. "
              "So the rule is: on the first fill that takes liquidity, pause in the same ledger, cancel the other "
              "offer, and stay paused until Ethan resumes. It happened twice (8 and 9 Nov) and held both times."),
    "end": ("Ended by Ethan on 9 Nov 2026 after session 3, because of his exams that week, not the P&L. 17.5 h "
            "running, 7.55 h quoting under the recorded rule: short of the 24 h and the hard stop. No more live "
            "sessions are planned."),
}


class Unavailable(Exception):
    pass


def live_view(report_path: Path, data_dir: Path, now: datetime) -> dict:
    base = {"label": LABEL, "network": "mainnet", "report": str(report_path), "data_dir": str(data_dir),
            "generated_at": iso(now), "rules": RULES, "uptime_rule": UPTIME_RULE, "explorer": EXPLORER}
    if not Path(report_path).exists():
        return {**base, "enabled": False,
                "reason": f"No live report at {report_path}. It is written by: python scripts/xrpl_live_report.py"}
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        periods = report["periods"]
    except (OSError, ValueError, KeyError) as e:
        return {**base, "enabled": False, "reason": f"Couldn't read the live report {report_path}: {e}"}
    try:
        read = [_read_period(p, Path(data_dir)) for p in periods]
    except Unavailable as e:
        return {**base, "enabled": False, "reason": str(e)}
    return {**base, "enabled": True, **_assemble(periods, read)}


# ---------------------------------------------------------------- databases, read-only

def _open(path: Path, role: str) -> sqlite3.Connection:
    if not path.exists():
        raise Unavailable(f"No database at {path}. Set DEBTLAB_XRPL_LIVE_DIR to the folder holding the live and "
                          "paper databases (for example C:\\debtlab-data).")
    try:
        conn = open_readonly(path)
        conn.execute("BEGIN")          # one read transaction: everything from the same moment
        table = role.split("-")[1]
        meta = dict(conn.execute(f"SELECT key, value FROM {table}_meta").fetchall())
    except sqlite3.Error as e:
        raise Unavailable(f"Couldn't read {path} as the {role} database: {e}") from e
    if meta.get("role") != role or meta.get("network") != "mainnet":
        conn.close()
        raise Unavailable(f"{path} is not a mainnet {role} database (role {meta.get('role')}, "
                          f"network {meta.get('network')}).")
    return conn


def _read_period(p: dict, data_dir: Path) -> dict:
    live = _open(data_dir / p["databases"]["live"], "xrpl-live")
    try:
        paper = _open(data_dir / p["databases"]["paper"], "xrpl-paper")
    except Unavailable:
        live.close()
        raise
    try:
        return _period_from_db(p, live, paper)
    except sqlite3.Error as e:
        raise Unavailable(f"Couldn't read {p['period']}'s databases: {e}") from e
    finally:
        live.close()
        paper.close()


def _intervals(times: list[datetime], lo: datetime, hi: datetime) -> list[list[datetime]]:
    """Quoting time by the recorded rule: between consecutive quotes no more than 30 s apart, clipped to [lo, hi]."""
    out: list[list[datetime]] = []
    for a, b in zip(times, times[1:]):
        if (b - a).total_seconds() > GAP_S:
            continue
        a, b = max(a, lo), min(b, hi)
        if b <= a:
            continue
        if out and out[-1][1] == a:
            out[-1][1] = b
        else:
            out.append([a, b])
    return out


def _period_from_db(p: dict, live: sqlite3.Connection, paper: sqlite3.Connection) -> dict:
    opened = parse(p["opened_at"])
    runs = [dict(r) for r in live.execute("SELECT * FROM live_runs ORDER BY id")]
    quoted = [parse(r[0]) for r in paper.execute("SELECT at FROM paper_decisions WHERE quoted = 1 ORDER BY id")]
    states = [dict(r) for r in live.execute("SELECT * FROM live_state_changes ORDER BY id")]

    sessions = []
    for i, (r, s) in enumerate(zip(runs, p["sessions"])):
        a = opened if i == 0 else parse(r["started_at"])
        b = parse(r["stopped_at"]) if r["stopped_at"] else parse(p["end"])
        quoting = _intervals(quoted, a, b)
        paused = []
        for k, st in enumerate(states):
            t = parse(st["changed_at"])
            if st["state"] != "paused" or not a <= t <= b:
                continue
            nxt = next((parse(x["changed_at"]) for x in states[k + 1:]), None)
            paused.append({"from": iso(t), "to": iso(min(nxt, b) if nxt else b), "by": st["changed_by"],
                           "reason": st["reason"]})
        sessions.append({"session": s["session"], "started": r["started_at"], "stopped": r["stopped_at"],
                         "stop_reason": r["stop_reason"],
                         "running_h": (b - parse(r["started_at"])).total_seconds() / 3600,
                         "quoting_h": sum((y - x).total_seconds() for x, y in quoting) / 3600,
                         "quoting": [[iso(x), iso(y)] for x, y in quoting], "paused": paused})

    # the budget's balances through time, and when either side went below zero (R21)
    journal = [(parse(r["at"]), r["ledger_index"], D(r["xrp"]), D(r["rlusd"]), r["kind"])
               for r in live.execute("SELECT at, ledger_index, xrp, rlusd, kind FROM live_journal ORDER BY id")]
    below, x, rl, cur = [], ZERO, ZERO, None
    for t, ledger, dx, dr, _kind in journal:
        x, rl = x + dx, rl + dr
        neg = x < 0 or rl < 0
        if neg and cur is None:
            cur = {"from": iso(t), "ledger": ledger, "lowest_xrp": x, "lowest_rlusd": rl}
            below.append(cur)
        elif neg:
            cur["lowest_xrp"], cur["lowest_rlusd"] = min(cur["lowest_xrp"], x), min(cur["lowest_rlusd"], rl)
        elif cur is not None:
            cur["to"], cur = iso(t), None
    if cur is not None:
        cur["to"] = sessions[-1]["stopped"] if sessions else p["end"]

    # fills, with the two effects that make live trade more than paper
    double = double_offer_fills(live)
    fills = [dict(r) for r in live.execute(
        "SELECT f.*, o.paper_order_id, o.price AS order_price FROM live_fills f LEFT JOIN live_orders o ON o.id = f.order_id "
        "ORDER BY f.ledger_index, f.id")]
    first_order: dict = {}
    for f in fills:
        f["double_offer"] = f["id"] in double
        po = f["paper_order_id"]
        first_order.setdefault(po, f["order_id"])
        f["refill"] = po is not None and f["order_id"] != first_order[po] and not f["double_offer"]

    marks = [(parse(r[0]), D(r[1])) for r in paper.execute(
        "SELECT at, fair FROM paper_decisions WHERE fair IS NOT NULL ORDER BY id")]
    return {"sessions": sessions, "below_zero": below, "fills": fills, "journal": journal, "marks": marks}


# ---------------------------------------------------------------- assembling the figures

def _s(x) -> str:
    return plain(D(x))


def _split(pnl: dict, usd_per_eur: Decimal) -> dict:
    out = {k: D(pnl[k]) for k in ("spread", "fees", "inventory_quoting", "inventory_paused_in_session",
                                  "inventory_stopped_between_sessions", "total")}
    out["quoting"] = out["spread"] + out["fees"] + out["inventory_quoting"]
    out["not_quoting"] = out["inventory_paused_in_session"] + out["inventory_stopped_between_sessions"]
    out["total_eur"] = out["total"] / usd_per_eur
    return out


def _account(p: dict, which: str) -> tuple[Decimal, Decimal, Decimal]:
    b = p["budget"][which]
    return D(b["xrp"]) + D(p["set_aside"]["xrp"]), D(b["rlusd"]) + D(p["set_aside"]["rlusd"]), D(b["mid"])


def _path(periods: list[dict], read: list[dict], start_value: Decimal) -> list[dict]:
    """The whole account's P&L through time against the reference mid: the budget's balances from the journal,
    plus what was set aside, marked at every paper decision's mid; one point a minute. Each period opens and
    ends on the report's own marks, so the last point is the report's figure."""
    pts: list[dict] = []
    for p, r in zip(periods, read):
        ax, ar = D(p["set_aside"]["xrp"]), D(p["set_aside"]["rlusd"])
        opened, end = parse(p["opened_at"]), parse(p["end"])

        def point(t, xrp, rlusd, mid):
            pts.append({"t": iso(t), "pnl": float((xrp + ax) * mid + rlusd + ar - start_value), "mid": float(mid),
                        "period": p["period"]})
        o = p["budget"]["open"]
        point(opened, D(o["xrp"]), D(o["rlusd"]), D(o["mid"]))
        j, x, rl, last_bucket = 0, ZERO, ZERO, None
        journal = r["journal"]
        for t, mid in r["marks"]:
            if t <= opened or t >= end:
                continue
            while j < len(journal) and journal[j][0] <= t:
                x, rl, j = x + journal[j][2], rl + journal[j][3], j + 1
            bucket = int(t.timestamp() // POINT_S)
            if bucket == last_bucket:
                pts.pop()
            last_bucket = bucket
            point(t, x, rl, mid)
        e = p["budget"]["end"]
        point(end, D(e["xrp"]), D(e["rlusd"]), D(e["mid"]))
    # which session each point falls in: the breaks in the chart are the time between sessions, nothing else
    wins = [(s["session"], parse(s["started"]), parse(s["stopped"])) for r in read for s in r["sessions"]]
    for q in pts:
        t = parse(q["t"])
        q["session"] = next((n for n, a, b in wins if a <= t <= b), None) or min(
            wins, key=lambda w: min(abs((t - w[1]).total_seconds()), abs((t - w[2]).total_seconds())))[0]
    for k, q in enumerate(pts):
        prev = pts[k - 1] if k else None
        q["gap_before"] = prev is not None and prev["session"] != q["session"]
        q["unmarked_before"] = (prev is not None and not q["gap_before"]
                                and (parse(q["t"]) - parse(prev["t"])).total_seconds() > BREAK_S)
    return pts


def _assemble(periods: list[dict], read: list[dict]) -> dict:
    eur = D(periods[0]["usd_per_eur"])
    first, last = periods[0], periods[-1]
    ox, orl, omid = _account(first, "open")
    ex, erl, emid = _account(last, "end")
    start_value, end_value = ox * omid + orl, ex * emid + erl
    account_pnl = end_value - start_value
    hold = ox * emid + orl - start_value

    # the whole account's P&L, part by part: each budget, each set-aside, and the time between periods
    parts = []
    for i, p in enumerate(periods):
        o_mid, e_mid = D(p["budget"]["open"]["mid"]), D(p["budget"]["end"]["mid"])
        if i:
            px, _, pmid = _account(periods[i - 1], "end")
            parts.append({"part": f"between {periods[i - 1]['period']} and {p['period']}", "rlusd": px * (o_mid - pmid)})
        parts.append({"part": f"{p['period']}: the budget", "rlusd": D(p["pnl_rlusd"]["total"])})
        parts.append({"part": f"{p['period']}: set aside", "rlusd": D(p["set_aside"]["xrp"]) * (e_mid - o_mid)})
    parts_total = sum((x["rlusd"] for x in parts), ZERO)

    splits = []
    for p in periods:
        sp = _split(p["pnl_rlusd"], eur)
        splits.append({"name": p["period"], "sessions": [s["session"] for s in p["sessions"]],
                       **{k: _s(v) for k, v in sp.items()}})
    combined = {k: sum((D(s[k]) for s in splits), ZERO) for k in splits[0] if k not in ("name", "sessions")}
    windows = [{"period": p["period"], "window": w, "total": _s(v["total"]), "fills": v["fills"],
                "spread": _s(v["spread"]), "fees": _s(v["fees"]), "inventory_quoting": _s(v["inventory_quoting"]),
                "inventory_not_quoting": _s(v["inventory_not_quoting"])}
               for p in periods for w, v in p["pnl_by_window_rlusd"].items()]

    compare = []
    for p in periods:
        c, q, pa = p["compare"], p["per_paper_quote"], p["paper"]
        compare.append({
            "period": p["period"],
            "xrp": {"live": c["xrp"]["live"], "conservative": c["xrp"]["conservative"], "optimistic": c["xrp"]["optimistic"],
                    "live_capped": q["live_xrp_capped_at_one_paper_order"], "refills": q["of_which_refills"],
                    "double_offer_beyond": q["of_which_double_offer"], "double_offer_all": q["double_offer_xrp_all"]},
            "pnl": {"live": p["pnl_rlusd"]["total"], "conservative": pa["conservative"]["rlusd"]["total"],
                    "optimistic": pa["optimistic"]["rlusd"]["total"],
                    "live_quoting": _s(_split(p["pnl_rlusd"], eur)["quoting"]),
                    "conservative_quoting": _s(_quoting(pa["conservative"]["rlusd"])),
                    "optimistic_quoting": _s(_quoting(pa["optimistic"]["rlusd"]))},
            "fills": {"live": p["pnl_rlusd"]["fills"], "conservative": pa["conservative"]["rlusd"]["fills"],
                      "optimistic": pa["optimistic"]["rlusd"]["fills"]},
            "fees_xrp": c["fees"],
            "only": {"live_where_conservative_none": q["live_xrp_on_quotes_conservative_never_filled"],
                     "conservative_where_live_none": q["conservative_xrp_on_quotes_live_never_filled"]},
            "opening": {"live": p["budget"]["open"], "paper": pa["conservative"]["start"]},
        })

    # timeline and events
    sessions, events = [], []
    for p, r in zip(periods, read):
        for s in r["sessions"]:
            sessions.append({"period": p["period"], **s})
        for f in r["fills"]:
            if f["role"] == "taker":
                events.append({"kind": "taker", "at": f["at"], "ledger": f["ledger_index"], "tx": f["tx_hash"],
                               "url": EXPLORER + f["tx_hash"], "side": f["side"], "xrp": f["xrp"], "period": p["period"],
                               "text": f"A {f['side']} took liquidity ({_s(f['xrp'])} XRP): paused in the same ledger"})
        for b in r["below_zero"]:
            events.append({"kind": "breach", "at": b["from"], "to": b.get("to"), "ledger": b["ledger"],
                           "period": p["period"], "lowest_xrp": _s(b["lowest_xrp"]), "lowest_rlusd": _s(b["lowest_rlusd"]),
                           "text": f"Budget below zero (R21): XRP down to {_s(b['lowest_xrp'])}"})
    events.sort(key=lambda e: e["at"])

    fills = []
    for p, r in zip(periods, read):
        for f in r["fills"]:
            fills.append({"period": p["period"], "at": f["at"], "ledger": f["ledger_index"], "side": f["side"],
                          "role": f["role"], "xrp": f["xrp"], "rlusd": f["rlusd"],
                          "price": _s(D(f["rlusd"]) / D(f["xrp"])) if D(f["xrp"]) else None,
                          "mid": f["mid"], "tx": f["tx_hash"], "url": EXPLORER + f["tx_hash"], "order": f["order_id"],
                          "double_offer": f["double_offer"], "refill": f["refill"]})
    fills.sort(key=lambda f: (f["at"], f["ledger"]), reverse=True)

    running_h = sum(s["wall_h"] for p in periods for s in p["sessions"])
    quoting_h = sum(s["uptime_h"] for p in periods for s in p["sessions"])
    checks = _checks(periods, read, parts_total, account_pnl)

    return {
        "headline": {
            "account_pnl_rlusd": _s(account_pnl), "account_pnl_eur": _s(account_pnl / eur),
            "hold_rlusd": _s(hold), "vs_hold_rlusd": _s(account_pnl - hold), "vs_hold_eur": _s((account_pnl - hold) / eur),
            "start_value_rlusd": _s(start_value), "end_value_rlusd": _s(end_value),
            "mid_open": _s(omid), "mid_end": _s(emid), "xrp_move": _s(emid / omid - 1),
            "fills": len(fills), "taker_fills": sum(f["role"] == "taker" for f in fills),
            "xrp_traded": _s(sum((D(f["xrp"]) for f in fills), ZERO)),
            "running_h": running_h, "quoting_h": quoting_h, "target_h": 24,
            "usd_per_eur": _s(eur), "opened_at": first["opened_at"], "ended_at": last["end"],
            "account": first.get("account"),
            "balances": {"open": {"xrp": _s(ox), "rlusd": _s(orl)}, "end": {"xrp": _s(ex), "rlusd": _s(erl)}},
        },
        "whole_account": {"parts": [{"part": x["part"], "rlusd": _s(x["rlusd"])} for x in parts],
                          "total": _s(parts_total)},
        "split": {"periods": splits, "combined": {k: _s(v) for k, v in combined.items()}, "windows": windows},
        "compare": compare,
        "sessions": sessions,
        "events": events,
        "path": _path(periods, read, start_value),
        "fills": fills,
        "drawdown": [{"period": p["period"], "rlusd": p["drawdown_rlusd"], "from": p["drawdown_from"], "to": p["drawdown_to"]}
                     for p in periods],
        "checks": checks,
    }


def _quoting(r: dict) -> Decimal:
    return D(r["spread"]) + D(r["fees"]) + D(r["inventory_quoting"])


def _checks(periods: list[dict], read: list[dict], parts_total: Decimal, account_pnl: Decimal) -> list[dict]:
    out = []
    for p, r in zip(periods, read):
        db = sorted((f["tx_hash"], f["order_id"]) for f in r["fills"])
        rep = sorted((f["tx"], f["order"]) for f in p["fill_hashes"])
        out.append({"name": f"{p['period']}: fills in the databases match the report's hashes", "ok": db == rep,
                    "detail": f"{len(db)} in the databases, {len(rep)} in the report"})
        mine = sum(s["quoting_h"] for s in r["sessions"])
        out.append({"name": f"{p['period']}: quoting time recounted from the decisions", "ok": abs(mine - p["uptime_h"]) < 1e-6,
                    "detail": f"{mine:.4f} h here, {p['uptime_h']:.4f} h in the report"})
    for a, b in zip(periods, periods[1:]):
        same = _account(a, "end")[:2] == _account(b, "open")[:2]
        out.append({"name": f"{b['period']} opened on the balances {a['period']} ended with", "ok": same,
                    "detail": "budget plus set-aside, XRP and RLUSD"})
    out.append({"name": "the parts add up to the whole account", "ok": abs(parts_total - account_pnl) < Decimal("1e-12"),
                "detail": f"{plain(parts_total.quantize(Decimal('1e-6')))} against {plain(account_pnl.quantize(Decimal('1e-6')))} RLUSD"})
    return out



