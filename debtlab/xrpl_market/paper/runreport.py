"""The end-of-run paper report (M7 step 7, brief §7.5). PAPER: no order was placed on any ledger.

Read-only on both databases. A report covers a window of the run: by default from the accounts'
opening to a cutoff (decision F's 24 h of uptime), with what came after reported separately. For
each fill rule: balances at both ends, orders, fills, cancels, refusals, fees, the P&L split,
drawdown and markouts; for the run: uptime, sessions, gaps, limit events and why it didn't quote.

The P&L split is the one `account.pnl` uses (spread + fees + inventory = total, walking the
journal), with marks added at the window's ends, at UTC midnights and at every quote, so
inventory can be split by day and by whether the bot was quoting. Without that, the market's move
while the bot was stopped or the laptop in standby would be hidden inside the quoting hours: the
paper accounts hold their XRP through every gap, as a live account would.
"""

from __future__ import annotations

import bisect
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from debtlab.money import D, plain
from debtlab.xrpl_market.paper import LABEL, account
from debtlab.xrpl_market.paper.db import iso, parse
from debtlab.xrpl_market.paper.market import mid_after, read_market

ZERO = Decimal(0)
GAP_S = 30          # uptime: time with a quote and no gap over 30 s, as the monitor counts its own


def quoted_times(conn: sqlite3.Connection) -> list[tuple[datetime, Decimal]]:
    return [(parse(r["at"]), D(r["fair"])) for r in conn.execute(
        "SELECT at, fair FROM paper_decisions WHERE quoted = 1 ORDER BY id")]


def uptime_s(q: list[datetime], start: datetime, end: datetime) -> float:
    """Seconds between consecutive quotes no more than 30 s apart, clipped to [start, end]."""
    total = 0.0
    for a, b in zip(q, q[1:]):
        if (b - a).total_seconds() <= GAP_S:
            lo, hi = max(a, start), min(b, end)
            if hi > lo:
                total += (hi - lo).total_seconds()
    return total


def when_uptime_reaches(q: list[datetime], hours: float) -> datetime | None:
    total = 0.0
    for a, b in zip(q, q[1:]):
        d = (b - a).total_seconds()
        if d <= GAP_S:
            if total + d >= hours * 3600:
                return a + timedelta(seconds=hours * 3600 - total)
            total += d
    return None


def gaps(q: list[datetime], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    return [(a, b) for a, b in zip(q, q[1:]) if (b - a).total_seconds() > GAP_S and b > start and a < end]


@dataclass
class Bucket:
    spread: Decimal = ZERO
    fees: Decimal = ZERO
    inv_quoting: Decimal = ZERO
    inv_not_quoting: Decimal = ZERO
    fills: int = 0
    fee_count: int = 0
    fees_xrp: Decimal = ZERO
    bought_xrp: Decimal = ZERO
    sold_xrp: Decimal = ZERO

    @property
    def inventory(self) -> Decimal:
        return self.inv_quoting + self.inv_not_quoting

    @property
    def total(self) -> Decimal:
        return self.spread + self.fees + self.inventory


@dataclass
class Walk:
    """One account's journal walked with marks: buckets by window and UTC day, and the value path."""
    buckets: dict = field(default_factory=dict)          # (window, day) -> Bucket
    balances: dict = field(default_factory=dict)         # time label -> (xrp, rlusd, mid, value)
    path: list = field(default_factory=list)             # (time, value) at every mark and entry


def walk(conn: sqlite3.Connection, name: str, marks: list[tuple[datetime, Decimal]], windows: list[tuple[str, datetime, datetime]],
         q: list[datetime], labels: dict[datetime, list[str]]) -> Walk:
    rows = conn.execute("SELECT * FROM paper_journal WHERE account = ? ORDER BY id", (name,)).fetchall()
    o = rows[0]
    assert o["kind"] == "open"
    # a mark comes after entries at the same time, so a window's balances include everything up to its end
    items = [(parse(r["at"]), 0, D(r["mid"]), r) for r in rows[1:]] + [(t, 1, m, None) for t, m in marks]
    items.sort(key=lambda x: (x[0], x[1]))
    xrp, rlusd, prev, prev_t = D(o["xrp"]), D(o["rlusd"]), D(o["mid"]), parse(o["at"])
    w = Walk()
    end = windows[-1][2]

    def bucket(t: datetime) -> Bucket | None:
        for wname, a, b in windows:
            if a < t <= b:
                return w.buckets.setdefault((wname, (t - timedelta(microseconds=1)).date().isoformat()), Bucket())   # up to midnight is that day
        return None

    def quoting(a: datetime, b: datetime) -> bool:
        i = bisect.bisect_right(q, a) - 1               # last quote at or before a
        return 0 <= i < len(q) - 1 and q[i + 1] >= b and (q[i + 1] - q[i]).total_seconds() <= GAP_S

    w.balances["open"] = (xrp, rlusd, prev, account.value(xrp, rlusd, prev))
    w.path.append((prev_t, account.value(xrp, rlusd, prev)))
    for t, is_mark, mid, r in items:
        is_entry = not is_mark
        if t > end:
            break
        bk = bucket(t)
        if bk is not None:
            d = xrp * (mid - prev)
            if quoting(prev_t, t):
                bk.inv_quoting += d
            else:
                bk.inv_not_quoting += d
        if is_entry:
            dx, dr = D(r["xrp"]), D(r["rlusd"])
            if bk is not None:
                if r["kind"] == "fill":
                    bk.spread += dx * mid + dr
                    bk.fills += 1
                    if dx > 0:
                        bk.bought_xrp += dx
                    else:
                        bk.sold_xrp -= dx
                else:
                    bk.fees += dx * mid + dr
                    bk.fee_count += 1
                    bk.fees_xrp -= dx
            xrp, rlusd = xrp + dx, rlusd + dr
        prev, prev_t = mid, t
        v = account.value(xrp, rlusd, mid)
        w.path.append((t, v))
        if not is_entry:
            for lab in labels.get(t, ()):
                w.balances[lab] = (xrp, rlusd, mid, v)
    return w


def drawdown(path: list[tuple[datetime, Decimal]], a: datetime, b: datetime) -> tuple[Decimal, datetime | None, datetime | None]:
    peak, peak_t, worst, worst_peak_t, worst_t = None, None, ZERO, None, None
    for t, v in path:
        if t < a or t > b:
            continue
        if peak is None or v > peak:
            peak, peak_t = v, t
        if peak - v > worst:
            worst, worst_peak_t, worst_t = peak - v, peak_t, t
    return worst, worst_peak_t, worst_t


def markouts(conn, mon, name: str, a: datetime, b: datetime, horizons: list[int], now: datetime) -> dict:
    out = {h: {"rlusd": ZERO, "fills": 0, "pending": 0} for h in horizons}
    for f in conn.execute("SELECT * FROM paper_fills WHERE account = ? AND at > ? AND at <= ? ORDER BY at",
                          (name, iso(a), iso(b))):
        qx, p, at = D(f["xrp"]), D(f["price"]), parse(f["at"])
        for h in horizons:
            later = mid_after(mon, at + timedelta(seconds=h), now)
            if later is None:
                out[h]["pending"] += 1
                continue
            out[h]["rlusd"] += qx * (later[0] - p) if f["side"] == "buy" else qx * (p - later[0])
            out[h]["fills"] += 1
    return out


def orders(conn, name: str, a: datetime, b: datetime) -> dict:
    one = lambda sql, *args: conn.execute(sql, (name, iso(a), iso(b), *args)).fetchone()[0]
    return {
        "placed": one("SELECT COUNT(*) FROM paper_orders WHERE account = ? AND placed_at > ? AND placed_at <= ?"),
        "filled_in_full": one("SELECT COUNT(*) FROM paper_orders WHERE account = ? AND closed_at > ? AND closed_at <= ?"
                              " AND status = 'filled'"),
        "cancelled": one("SELECT COUNT(*) FROM paper_orders WHERE account = ? AND closed_at > ? AND closed_at <= ?"
                         " AND status = 'cancelled'"),
        "cancelled_part_filled": one("SELECT COUNT(*) FROM paper_orders WHERE account = ? AND closed_at > ? AND closed_at <= ?"
                                     " AND status = 'cancelled' AND CAST(filled AS REAL) > 0"),
    }


def _kind(text: str) -> str:
    """A reason without its figures, so like counts with like."""
    return re.sub(r"\s+", " ", re.sub(r"-?\d[\d.,E+-]*", "#", text)).strip()[:110]


def events(conn, a: datetime, b: datetime) -> dict:
    rows = conn.execute("SELECT * FROM paper_events WHERE at > ? AND at <= ? ORDER BY id", (iso(a), iso(b))).fetchall()
    kinds = Counter(r["kind"] for r in rows)
    refused = Counter()
    for r in rows:
        if r["kind"] == "order_refused":
            d = json.loads(r["detail"])
            refused[f"{d['account']}: " + "; ".join(_kind(x) for x in d["reasons"])] += 1
    states = [dict(r) for r in conn.execute("SELECT * FROM paper_state_changes WHERE changed_at > ? AND changed_at <= ?",
                                            (iso(a), iso(b)))]
    limit_changes = [dict(r) for r in conn.execute("SELECT * FROM paper_limit_changes WHERE changed_at <= ?", (iso(b),))]
    no_quote = Counter()
    for r in conn.execute("SELECT reasons FROM paper_decisions WHERE quoted = 0 AND at > ? AND at <= ?", (iso(a), iso(b))):
        rs = json.loads(r["reasons"])
        no_quote[_kind(rs[0]) if rs else "(none given)"] += 1
    return {"kinds": dict(kinds), "refused": dict(refused.most_common()), "state_changes": states,
            "limit_changes": limit_changes, "no_quote": dict(no_quote.most_common()),
            "risk_pauses": [s for s in states if s["changed_by"] == "risk check"]}


def outages(mon, a: datetime, b: datetime) -> dict:
    rows = mon.execute("SELECT kind, started_at, ended_at FROM xrpl_outages WHERE started_at <= ? AND"
                       " (ended_at IS NULL OR ended_at >= ?)", (iso(b), iso(a))).fetchall()
    secs = Counter()
    for r in rows:
        s, e = max(parse(r["started_at"]), a), min(parse(r["ended_at"]) if r["ended_at"] else b, b)
        secs[r["kind"]] += max((e - s).total_seconds(), 0)
    return {"count": len(rows), "seconds_by_kind": {k: round(v, 1) for k, v in secs.items()}}


def build(conn: sqlite3.Connection, mon: sqlite3.Connection, cfg: dict, cutoff_hours: float = 24.0,
          now: datetime | None = None) -> dict:
    accts = account.accounts(conn)
    opened = parse(accts[0]["opened_at"])
    qd = quoted_times(conn)
    q = [t for t, _ in qd]
    runs = [dict(r) for r in conn.execute("SELECT * FROM paper_runs ORDER BY id")]
    end = max(q[-1], *(parse(r["stopped_at"]) for r in runs if r["stopped_at"]))
    cut = when_uptime_reaches(q, cutoff_hours) or end
    now = now or end
    windows = [("main", opened - timedelta(microseconds=1), cut), ("after", cut, end)]
    day = datetime.combine(opened.date() + timedelta(days=1), datetime.min.time(), opened.tzinfo)
    midnights = []
    while day < end:
        midnights.append(day)
        day += timedelta(days=1)
    gp = gaps(q, opened, end)
    mk = {cut: read_market(mon, cut, cfg).mid, end: read_market(mon, end, cfg).mid}
    for m in midnights:
        mk[m] = read_market(mon, m, cfg).mid
    for t, m in qd:             # every quote is a mark, so no interval spans more than one quote-to-quote step
        mk.setdefault(t, m)
    marks = sorted((t, m) for t, m in mk.items() if m is not None)
    labels = {cut: ["cutoff"]}
    labels.setdefault(end, []).append("end")     # the same time if the run never reached the cutoff
    fx_cut, fx_end = read_market(mon, cut, cfg).fx, read_market(mon, end, cfg).fx
    horizons = cfg["paper"]["markout_seconds"]

    out = {"label": LABEL, "note": ("PAPER: no order was placed on any ledger. Fills are inferred from real mainnet trades under "
                                    "two rules; the truth is somewhere between the conservative and optimistic accounts, and "
                                    "neither models the queue ahead of us, so read fills as an upper bound."),
           "opened_at": iso(opened), "cutoff": iso(cut), "end": iso(end), "cutoff_hours": cutoff_hours,
           "uptime_h": {"main": uptime_s(q, opened, cut) / 3600, "after": uptime_s(q, cut, end) / 3600},
           "wall_h": {"main": (cut - opened).total_seconds() / 3600, "after": (end - cut).total_seconds() / 3600},
           "mid": {"open": plain(D(accts[0]["mid"])), "cutoff": plain(mk[cut]), "end": plain(mk[end])},
           "usd_per_eur": {"open": accts[0]["usd_per_eur"], "cutoff": plain(fx_cut.usd_per_eur), "end": plain(fx_end.usd_per_eur)},
           "runs": runs,
           "sessions": [], "gaps": [], "days": [d.date().isoformat() for d in midnights],
           "events": {"main": events(conn, opened - timedelta(microseconds=1), cut), "after": events(conn, cut, end)},
           "outages": {"main": outages(mon, opened, cut), "after": outages(mon, cut, end)},
           "accounts": []}
    for r in runs:
        a, b = parse(r["started_at"]), parse(r["stopped_at"]) if r["stopped_at"] else end
        qs = [t for t in q if a <= t <= b]
        out["sessions"].append({"run": r["id"], "started": r["started_at"], "stopped": r["stopped_at"],
                                "first_quote": iso(qs[0]) if qs else None, "last_quote": iso(qs[-1]) if qs else None,
                                "uptime_h": uptime_s(q, a, b) / 3600,
                                "uptime_h_main": uptime_s(q, a, min(b, cut)) / 3600 if a < cut else 0.0})
    for a, b in gp:
        out["gaps"].append({"from": iso(a), "to": iso(b), "minutes": round((b - a).total_seconds() / 60, 1),
                            "window": "main" if b <= cut else "after" if a >= cut else "both"})

    for acct in accts:
        name = acct["name"]
        w = walk(conn, name, marks, windows, q, labels)
        res = {"name": name, "rule": acct["rule"], "windows": {}}
        for wname, a, b in windows:
            start_bal = w.balances["open"] if wname == "main" else w.balances["cutoff"]
            end_bal = w.balances["cutoff"] if wname == "main" else w.balances["end"]
            fx = fx_cut if wname == "main" else fx_end
            eur = lambda x, fx=fx: x / fx.usd_per_eur
            tot = Bucket()
            days = {}
            for (wn, d), bk in sorted(w.buckets.items()):
                if wn != wname:
                    continue
                days[d] = bk
                for k in ("spread", "fees", "inv_quoting", "inv_not_quoting", "fills", "fee_count", "fees_xrp", "bought_xrp", "sold_xrp"):
                    setattr(tot, k, getattr(tot, k) + getattr(bk, k))
            change = end_bal[3] - start_bal[3]
            assert abs(tot.total - change) < Decimal("1e-20"), (name, wname, tot.total, change)
            hold = start_bal[0] * end_bal[2] + start_bal[1] - start_bal[3]     # held the window's opening balances
            dd, dd_peak, dd_at = drawdown(w.path, a, b)

            def split(bk: Bucket) -> dict:
                return {"spread": bk.spread, "fees": bk.fees, "inventory_quoting": bk.inv_quoting,
                        "inventory_not_quoting": bk.inv_not_quoting, "inventory": bk.inventory, "total": bk.total,
                        "fills": bk.fills, "bought_xrp": bk.bought_xrp, "sold_xrp": bk.sold_xrp,
                        "fee_txs": bk.fee_count, "fees_xrp": bk.fees_xrp}
            res["windows"][wname] = {
                "start": {"xrp": start_bal[0], "rlusd": start_bal[1], "mid": start_bal[2], "value_rlusd": start_bal[3]},
                "end": {"xrp": end_bal[0], "rlusd": end_bal[1], "mid": end_bal[2], "value_rlusd": end_bal[3]},
                "rlusd": split(tot), "eur": {k: eur(v) for k, v in split(tot).items() if isinstance(v, Decimal)
                                             and k not in ("bought_xrp", "sold_xrp", "fees_xrp")},
                "hold_rlusd": hold, "hold_eur": eur(hold), "vs_hold_rlusd": change - hold, "vs_hold_eur": eur(change - hold),
                "drawdown_rlusd": dd, "drawdown_eur": eur(dd), "drawdown_from": iso(dd_peak) if dd_peak else None,
                "drawdown_to": iso(dd_at) if dd_at else None,
                "days": {d: {"rlusd": split(bk), "eur_total": eur(bk.total)} for d, bk in days.items()},
                "orders": orders(conn, name, a, b),
                "markouts_rlusd": markouts(conn, mon, name, a, b, horizons, now + timedelta(seconds=max(horizons))),
            }
        res["value_eur"] = {"open": D(acct["start_eur"]),
                            "cutoff": w.balances["cutoff"][3] / fx_cut.usd_per_eur,
                            "end": w.balances["end"][3] / fx_end.usd_per_eur}
        out["accounts"].append(res)
    for wname, a, b in windows:
        out["uptime_h"][f"{wname}_by_day"] = {}
        for d in [opened.date() + timedelta(days=i) for i in range((b.date() - opened.date()).days + 1)]:
            lo = max(a, datetime.combine(d, datetime.min.time(), opened.tzinfo))
            hi = min(b, datetime.combine(d + timedelta(days=1), datetime.min.time(), opened.tzinfo))
            if hi > lo:
                out["uptime_h"][f"{wname}_by_day"][d.isoformat()] = uptime_s(q, lo, hi) / 3600
    return out


def _j(x):
    if isinstance(x, Decimal):
        return plain(x)
    if isinstance(x, dict):
        return {str(k): _j(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_j(v) for v in x]
    return x


def to_json(r: dict) -> str:
    return json.dumps(_j(r), indent=1, sort_keys=False)


def _f(x, places: int = 4) -> str:
    return "-" if x is None else f"{Decimal(str(x)):,.{places}f}"


def _t(s: str | None) -> str:
    return "-" if s is None else s[:19].replace("T", " ")


def render(r: dict) -> str:
    """Markdown. Display rounding only: the JSON beside it is exact."""
    A = {a["name"]: a for a in r["accounts"]}
    names = list(A)
    L = [f"# {r['label']}: M7 paper run report, RLUSD/XRP on mainnet data", "",
         f"**{r['note']}**", "",
         "Generated from the paper and monitor databases by `scripts/xrpl_paper_report.py` (read-only). "
         "Times are UTC. Euro figures convert RLUSD at 1 USD (decision D) and the ECB rate at the end of each window.", "",
         "## Window", "",
         f"- **Reported result:** the first {r['cutoff_hours']:g} h of uptime (decision F), from the accounts' opening "
         f"{_t(r['opened_at'])} to {_t(r['cutoff'])}: {r['wall_h']['main']:.2f} h of wall-clock time, "
         f"{r['uptime_h']['main']:.3f} h of uptime. The cutoff was fixed by Ethan before the results were looked at.",
         f"- **Separate check, not part of the result:** {_t(r['cutoff'])} to {_t(r['end'])}, {r['uptime_h']['after']:.3f} h of uptime.",
         f"- Uptime is time with a quote and no gap over 30 s between quotes. Mid (reference): {_f(r['mid']['open'], 6)} at opening, "
         f"{_f(r['mid']['cutoff'], 6)} at the cutoff ({(Decimal(r['mid']['cutoff']) / Decimal(r['mid']['open']) - 1) * 100:+.2f}%), "
         f"{_f(r['mid']['end'], 6)} at the end. EUR/USD {r['usd_per_eur']['open']} at opening, {r['usd_per_eur']['cutoff']} at the cutoff.", "",
         f"## Headline (first {r['cutoff_hours']:g} h of uptime)", "",
         "| EUR | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    rows = [("Start value", lambda a: a["value_eur"]["open"]),
            ("End value", lambda a: a["value_eur"]["cutoff"]),
            ("**Total P&L**", lambda a: a["windows"]["main"]["eur"]["total"]),
            ("Spread captured", lambda a: a["windows"]["main"]["eur"]["spread"]),
            ("Network fees", lambda a: a["windows"]["main"]["eur"]["fees"]),
            ("Inventory, while quoting", lambda a: a["windows"]["main"]["eur"]["inventory_quoting"]),
            ("Inventory, while not quoting", lambda a: a["windows"]["main"]["eur"]["inventory_not_quoting"]),
            ("Doing nothing (held the opening balances)", lambda a: a["windows"]["main"]["hold_eur"]),
            ("**Bot versus doing nothing**", lambda a: a["windows"]["main"]["vs_hold_eur"]),
            ("Largest drawdown", lambda a: -a["windows"]["main"]["drawdown_eur"])]
    for label, fn in rows:
        L.append(f"| {label} | " + " | ".join(_f(fn(A[n])) for n in names) + " |")
    L += ["", "Total P&L = spread + fees + inventory exactly (checked when the report is built). "
          "Start value is EUR 50 at the opening rate; end value is at the cutoff's rate, so the two differ by the EUR/USD move "
          "as well as the P&L. The P&L lines are in RLUSD converted at the cutoff's rate.", ""]
    for wname, title in (("main", f"Detail: first {r['cutoff_hours']:g} h of uptime (the result)"), ("after", "Separate check: after the cutoff")):
        L += [f"## {title}", ""]
        L += ["| | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
        W = {n: A[n]["windows"][wname] for n in names}
        det = [("Start XRP / RLUSD", lambda w: f"{_f(w['start']['xrp'], 6)} / {_f(w['start']['rlusd'], 6)}"),
               ("End XRP / RLUSD", lambda w: f"{_f(w['end']['xrp'], 6)} / {_f(w['end']['rlusd'], 6)}"),
               ("Start / end value, RLUSD", lambda w: f"{_f(w['start']['value_rlusd'])} / {_f(w['end']['value_rlusd'])}"),
               ("Orders placed", lambda w: f"{w['orders']['placed']:,}"),
               ("Filled in full", lambda w: f"{w['orders']['filled_in_full']:,}"),
               ("Cancelled (of which part-filled)", lambda w: f"{w['orders']['cancelled']:,} ({w['orders']['cancelled_part_filled']:,})"),
               ("Fills", lambda w: f"{w['rlusd']['fills']:,}"),
               ("XRP bought / sold", lambda w: f"{_f(w['rlusd']['bought_xrp'], 2)} / {_f(w['rlusd']['sold_xrp'], 2)}"),
               ("Fee transactions / XRP", lambda w: f"{w['rlusd']['fee_txs']:,} / {_f(w['rlusd']['fees_xrp'], 6)}"),
               ("Spread / fees, RLUSD", lambda w: f"{_f(w['rlusd']['spread'])} / {_f(w['rlusd']['fees'])}"),
               ("Inventory quoting / not, RLUSD", lambda w: f"{_f(w['rlusd']['inventory_quoting'])} / {_f(w['rlusd']['inventory_not_quoting'])}"),
               ("Total P&L, RLUSD / EUR", lambda w: f"{_f(w['rlusd']['total'])} / {_f(w['eur']['total'])}"),
               ("Doing nothing, RLUSD", lambda w: _f(w["hold_rlusd"])),
               ("Largest drawdown, RLUSD", lambda w: f"{_f(w['drawdown_rlusd'])} ({_t(w['drawdown_from'])} to {_t(w['drawdown_to'])})")]
        for h in ("60", "300"):
            det.append((f"Markout {h} s, RLUSD (fills)", lambda w, h=h: f"{_f(w['markouts_rlusd'][int(h)]['rlusd'])} "
                        f"({w['markouts_rlusd'][int(h)]['fills']:,})" if int(h) in w["markouts_rlusd"] else
                        f"{_f(w['markouts_rlusd'][h]['rlusd'])} ({w['markouts_rlusd'][h]['fills']:,})"))
        for label, fn in det:
            L.append(f"| {label} | " + " | ".join(fn(W[n]) for n in names) + " |")
        L.append("")
        days = sorted(set().union(*(W[n]["days"].keys() for n in names)))
        if days:
            L += ["By UTC day, RLUSD:", "", "| Day | Uptime h | " + " | ".join(f"{n}: fills / spread / fees / inventory / total" for n in names) + " |",
                  "|---|---:|" + "---:|" * len(names)]
            for d in days:
                cells = []
                for n in names:
                    x = W[n]["days"].get(d)
                    cells.append("-" if x is None else f"{x['rlusd']['fills']:,} / {_f(x['rlusd']['spread'])} / {_f(x['rlusd']['fees'])}"
                                 f" / {_f(x['rlusd']['inventory'])} / {_f(x['rlusd']['total'])}")
                L.append(f"| {d} | {r['uptime_h'][f'{wname}_by_day'].get(d, 0):.2f} | " + " | ".join(cells) + " |")
            L.append("")
        ev = r["events"][wname]
        L += [f"Limit events: {len(ev['risk_pauses'])} pauses by the risk check"
              + (": " + "; ".join(f"{_t(s['changed_at'])} {s['reason']}" for s in ev["risk_pauses"]) if ev["risk_pauses"] else "")
              + f"; {sum(ev['refused'].values())} orders refused.", ""]
        if ev["refused"]:
            L += ["| Refused orders, by reason | Count |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in ev["refused"].items()] + [""]
        if ev["no_quote"]:
            L += ["| No quote, by reason | Decisions |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in list(ev["no_quote"].items())[:12]] + [""]
        oc = r["outages"][wname]
        L += [f"Monitor outages overlapping the window: {oc['count']}, " + ", ".join(f"{k} {v / 60:.1f} min" for k, v in oc["seconds_by_kind"].items()) + ".", ""]
    L += ["## Sessions and gaps", "", "| Run | Started | Stopped | First quote | Last quote | Uptime h | In the result |", "|---|---|---|---|---|---:|---:|"]
    for s in r["sessions"]:
        L.append(f"| {s['run']} | {_t(s['started'])} | {_t(s['stopped'])} | {_t(s['first_quote'])} | {_t(s['last_quote'])} | "
                 f"{s['uptime_h']:.3f} | {s['uptime_h_main']:.3f} |")
    L += ["", "| Gap from | to | Minutes | Window |", "|---|---|---:|---|"]
    for g in r["gaps"]:
        L.append(f"| {_t(g['from'])} | {_t(g['to'])} | {g['minutes']:,.1f} | {g['window']} |")
    lc = r["events"]["main"]["limit_changes"]
    L += ["", "Limits in force (never changed during the run): " + ", ".join(f"{x['name']} {x['value']}" for x in lc) + ".", ""]
    return "\n".join(L)
