"""GET /api/live and the dashboard's *Live trading* section: the M8 mainnet run after it ended. Read-only.

A small world with known answers: two budget periods, three sessions, a refill, a double-offer fill that takes
the budget below zero (R21), and a fill that took liquidity and paused the bot (R20). The report JSON is
written here to match, the way scripts/xrpl_live_report.py writes the real one.
"""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from debtlab.api.app import create_app, render_page
from debtlab.config import Settings
from debtlab.xrpl_market.live import db as live_db
from debtlab.xrpl_market.liveview import EXPLORER, LABEL, live_view
from debtlab.xrpl_market.paper import db as paper_db

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
H = lambda n: f"{n:064X}"  # noqa: E731


def iso(t):
    return t.isoformat(timespec="milliseconds")


def make_live(path, opened, aside, runs, states, orders, journal, fills):
    c = live_db.open_db(path, "mainnet")
    with c:
        c.execute("INSERT INTO live_account (id, address, opened_at, ledger_index, mid, usd_per_eur, aside_xrp, aside_rlusd) "
                  "VALUES (1, 'rTEST', ?, 1, '1', '1.25', ?, ?)", (iso(opened), aside[0], aside[1]))
        for i, (a, b) in enumerate(runs, 1):
            c.execute("INSERT INTO live_runs VALUES (?, ?, ?, 'stopped by user')", (i, iso(a), iso(b)))
        for st, at, by in states:
            c.execute("INSERT INTO live_state_changes (state, changed_at, changed_by, reason) VALUES (?, ?, ?, 'test')",
                      (st, iso(at), by))
        for o in orders:   # id, paper order, side, sequence, replaces, placed ledger, applied ledger
            c.execute("INSERT INTO live_orders (id, paper_order_id, side, price, size, sequence, expiration, tx_hash, replaces, "
                      "status, placed_at, placed_ledger, applied_ledger) VALUES (?, ?, ?, '1', '4', ?, 0, ?, ?, 'filled', ?, ?, ?)",
                      (o[0], o[1], o[2], o[3], H(o[3]), o[4], iso(opened), o[5], o[6]))
        for k, (at, ledger, kind, x, r, mid) in enumerate(journal):
            c.execute("INSERT INTO live_journal (at, ledger_index, kind, xrp, rlusd, mid, ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (iso(at), ledger, kind, x, r, mid, f"j{k}"))
        for f in fills:    # id, order, ledger, at, tx, role, side, xrp, rlusd
            c.execute("INSERT INTO live_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '1')", (*f[:3], iso(f[3]), *f[4:]))
    c.close()


def make_paper(path, decisions):
    c = paper_db.open_db(path)
    with c:
        c.execute("INSERT OR REPLACE INTO paper_meta VALUES ('network', 'mainnet')")
        for at, quoted, fair in decisions:
            c.execute("INSERT INTO paper_decisions (at, quoted, reasons, fair, components) VALUES (?, ?, '[]', ?, '{}')",
                      (iso(at), quoted, fair))
    c.close()


def decisions(start, quoted_s, then_s, fair0, fair1):
    out, n = [], (quoted_s + then_s) // 4
    for k in range(n + 1):
        fair = Decimal(fair0) + (Decimal(fair1) - Decimal(fair0)) * k / n
        out.append((start + timedelta(seconds=4 * k), 1 if 4 * k <= quoted_s else 0, str(fair)))
    return out


def pnl(spread, fees, invq, paused, stopped, total, fills):
    return {"spread": spread, "fees": fees, "inventory_quoting": invq, "inventory_paused_in_session": paused,
            "inventory_stopped_between_sessions": stopped, "inventory_not_quoting": str(Decimal(paused) + Decimal(stopped)),
            "total": total, "fills": fills}


def paper_side(total, fills):
    return {"rlusd": {"spread": "0.1", "fees": "-0.01", "inventory_quoting": "-0.09", "total": total, "fills": fills},
            "start": {"xrp": "5", "rlusd": "15", "mid": "1", "value_rlusd": "20"}}


def period(name, files, opened, end, aside, bopen, bend, sessions, money, fill_hashes):
    return {"period": name, "databases": dict(zip(("live", "paper"), files)), "account": "rTEST",
            "opened_at": iso(opened), "usd_per_eur": "1.25", "set_aside": dict(zip(("xrp", "rlusd"), aside)), "end": iso(end),
            "sessions": sessions, "uptime_h": sum(s["uptime_h"] for s in sessions), "wall_h": sum(s["wall_h"] for s in sessions),
            "budget": {"open": dict(zip(("xrp", "rlusd", "mid"), bopen)), "end": dict(zip(("xrp", "rlusd", "mid"), bend))},
            "pnl_rlusd": money, "pnl_by_window_rlusd": {s["session"]: money for s in sessions[:1]},
            "drawdown_rlusd": "0.5", "drawdown_from": iso(opened), "drawdown_to": iso(end),
            "compare": {"xrp": {"live": "12", "conservative": "4", "optimistic": "6"},
                        "fees": {"live": "0.01", "conservative": "0.005", "optimistic": "0.004"}},
            "per_paper_quote": {"live_xrp_capped_at_one_paper_order": "4", "of_which_refills": "4", "of_which_double_offer": "4",
                                "double_offer_xrp_all": "4", "live_xrp_on_quotes_conservative_never_filled": "0",
                                "conservative_xrp_on_quotes_live_never_filled": "0"},
            "paper": {"conservative": paper_side("-0.2", 2), "optimistic": paper_side("-0.3", 3)},
            "fill_hashes": fill_hashes}


def session(name, a, b, quoting_s):
    return {"session": name, "started": iso(a), "stopped": iso(b), "wall_h": (b - a).total_seconds() / 3600,
            "uptime_h": quoting_s / 3600}


def build(tmp_path, *, drop_fill=False):
    """Period 1: opens 13 XRP + 11 RLUSD at mid 1 (3 XRP of it set aside); one paper order filled by three live orders:
    the first, a refill, and a replacement filled after its old offer filled unseen, which takes the budget to -2 XRP.
    Period 2: a taker buy pauses session 2; session 3 quotes 100 s. The mid ends at 0.8."""
    data = tmp_path / "data"
    data.mkdir()
    s1 = (T0 - timedelta(seconds=10), T0 + timedelta(seconds=600))
    f1 = [("f1", "A", 15, T0 + timedelta(seconds=100), H(1001), "maker", "sell", "4", "4.1"),
          ("f2", "B", 27, T0 + timedelta(seconds=200), H(1002), "maker", "sell", "4", "4.1"),
          ("f3", "C", 29, T0 + timedelta(seconds=300), H(1003), "maker", "sell", "4", "4.2")]
    make_live(data / "l1.sqlite3", T0, ("3", "1"), [s1], [("running", T0, "Ethan")],
              [("A", "P-1", "sell", 101, None, 10, 11), ("B", "P-1", "sell", 102, 101, 20, 22), ("C", "P-1", "sell", 103, 102, 26, 28)],
              [(T0, 1, "open", "10", "10", "1"), *[(f[3], f[2], "maker_fill", "-4", f[8], "1") for f in f1]], f1)
    make_paper(data / "p1.sqlite3", decisions(T0, 400, 196, "1.0", "0.9"))

    s2, s3 = (T1 - timedelta(seconds=5), T1 + timedelta(seconds=300)), (T1 + timedelta(seconds=3600), T1 + timedelta(seconds=3900))
    f2 = [("g1", "D", 50, T1 + timedelta(seconds=200), H(2001), "taker", "buy", "2", "2.2"),
          ("g2", "E", 70, T1 + timedelta(seconds=3700), H(2002), "maker", "buy", "3", "3.3")]
    make_live(data / "l2.sqlite3", T1, ("0", "3.4"), [s2, s3],
              [("running", T1, "Ethan"), ("paused", T1 + timedelta(seconds=200), "bot"), ("running", T1 + timedelta(seconds=3610), "Ethan")],
              [("D", "P-9", "buy", 201, None, 49, 50), ("E", "P-10", "buy", 202, None, 69, 69)],
              [(T1, 1, "open", "1", "20", "1.1"), *[(f[3], f[2], f[5] + "_fill", f[7], "-" + f[8], "1") for f in f2]], f2)
    make_paper(data / "p2.sqlite3", decisions(T1, 200, 100, "1.1", "1.0") + decisions(T1 + timedelta(seconds=3610), 100, 288, "0.9", "0.8"))

    hashes = lambda fs: [{"tx": f[4], "order": f[1]} for f in fs]  # noqa: E731
    report = {"label": "MAINNET: real funds", "periods": [
        period("period 1", ("l1.sqlite3", "p1.sqlite3"), T0, s1[1], ("3", "1"), ("10", "10", "1"), ("-2", "22.4", "0.9"),
               [session("session 1", *s1, 400)], pnl("0.7", "-0.1", "0", "0", "0", "0.6", 3), hashes(f1)),
        period("period 2", ("l2.sqlite3", "p2.sqlite3"), T1, s3[1], ("0", "3.4"), ("1", "20", "1.1"), ("6", "14.5", "0.8"),
               [session("session 2", *s2, 200), session("session 3", *s3, 100)], pnl("0.2", "-0.05", "-0.65", "-0.3", "-1", "-1.8", 2),
               hashes(f2[:1] if drop_fill else f2)),
    ]}
    rpath = tmp_path / "m8_live_report.json"
    rpath.write_text(json.dumps(report), encoding="utf-8")
    return rpath, data


def settings(tmp_path, report, data):
    return Settings(db_path=tmp_path / "t.sqlite3", data_mode="offline", max_curve_age_hours=96,
                    settlement_date=date(2026, 11, 1), instruments_file=ROOT / "config" / "instruments.json",
                    host="127.0.0.1", port=8000, xrpl_db_path=tmp_path / "none.sqlite3",
                    live_report_path=report, xrpl_live_dir=data)


NOW = T1 + timedelta(days=1)
D = Decimal


def test_no_report_is_disabled_and_nothing_is_created(tmp_path):
    v = live_view(tmp_path / "missing.json", tmp_path / "data", NOW)
    assert v["enabled"] is False and "scripts/xrpl_live_report.py" in v["reason"] and v["label"] == LABEL
    assert not (tmp_path / "data").exists()


def test_a_missing_database_says_which_and_creates_nothing(tmp_path):
    report, data = build(tmp_path)
    (data / "p2.sqlite3").unlink()
    v = live_view(report, data, NOW)
    assert v["enabled"] is False and "p2.sqlite3" in v["reason"] and "DEBTLAB_XRPL_LIVE_DIR" in v["reason"]
    assert not (data / "p2.sqlite3").exists()


def test_a_testnet_database_is_refused(tmp_path):
    report, data = build(tmp_path)
    (data / "l1.sqlite3").unlink()
    live_db.open_db(data / "l1.sqlite3", "testnet").close()
    v = live_view(report, data, NOW)
    assert v["enabled"] is False and "not a mainnet xrpl-live database" in v["reason"]


def test_headline_and_whole_account(tmp_path):
    v = live_view(*build(tmp_path), NOW)
    assert v["enabled"] and v["label"] == "MAINNET: real funds, EUR 50 experiment, run ended"
    h = v["headline"]
    assert D(h["start_value_rlusd"]) == 24 and D(h["end_value_rlusd"]) == D("22.7")
    assert D(h["account_pnl_rlusd"]) == D("-1.3") and D(h["account_pnl_eur"]) == D("-1.3") / D("1.25")
    assert D(h["hold_rlusd"]) == D("-2.6") and D(h["vs_hold_rlusd"]) == D("1.3")
    assert D(h["xrp_move"]) == D("-0.2") and h["fills"] == 5 and h["taker_fills"] == 1
    assert h["running_h"] == pytest.approx((610 + 305 + 300) / 3600) and h["quoting_h"] == pytest.approx(700 / 3600)
    parts = {p["part"]: D(p["rlusd"]) for p in v["whole_account"]["parts"]}
    assert parts == {"period 1: the budget": D("0.6"), "period 1: set aside": D("-0.3"),
                     "between period 1 and period 2": D("0.2"), "period 2: the budget": D("-1.8"), "period 2: set aside": 0}
    assert D(v["whole_account"]["total"]) == D("-1.3")
    assert all(c["ok"] for c in v["checks"]), v["checks"]


def test_split_by_period_and_combined(tmp_path):
    v = live_view(*build(tmp_path), NOW)
    p1, p2 = v["split"]["periods"]
    assert (D(p1["quoting"]), D(p1["not_quoting"])) == (D("0.6"), 0)
    assert (D(p2["quoting"]), D(p2["not_quoting"]), D(p2["total"])) == (D("-0.5"), D("-1.3"), D("-1.8"))
    c = v["split"]["combined"]
    assert (D(c["spread"]), D(c["fees"]), D(c["inventory_quoting"])) == (D("0.9"), D("-0.15"), D("-0.65"))
    assert (D(c["inventory_paused_in_session"]), D(c["inventory_stopped_between_sessions"]), D(c["total"])) == (D("-0.3"), -1, D("-1.2"))
    assert D(c["total_eur"]) == D("-1.2") / D("1.25")


def test_live_against_paper(tmp_path):
    v = live_view(*build(tmp_path), NOW)
    c = v["compare"][0]
    assert c["xrp"] == {"live": "12", "conservative": "4", "optimistic": "6", "live_capped": "4", "refills": "4",
                        "double_offer_beyond": "4", "double_offer_all": "4"}
    assert (c["pnl"]["live"], c["pnl"]["conservative"], c["pnl"]["optimistic"]) == ("0.6", "-0.2", "-0.3")
    assert D(c["pnl"]["conservative_quoting"]) == 0 and c["fills"] == {"live": 3, "conservative": 2, "optimistic": 3}


def test_timeline_events_and_rules(tmp_path):
    v = live_view(*build(tmp_path), NOW)
    s1, s2, s3 = v["sessions"]
    assert s1["quoting_h"] == pytest.approx(400 / 3600) and s1["quoting"] == [[iso(T0), iso(T0 + timedelta(seconds=400))]]
    assert s1["paused"] == [] and s3["quoting_h"] == pytest.approx(100 / 3600)
    (p,) = s2["paused"]
    assert p["from"] == iso(T1 + timedelta(seconds=200)) and p["to"] == iso(T1 + timedelta(seconds=300)) and p["by"] == "bot"
    breach, taker = v["events"]
    assert breach["kind"] == "breach" and breach["at"] == iso(T0 + timedelta(seconds=300)) and breach["lowest_xrp"] == "-2"
    assert breach["to"] == iso(T0 + timedelta(seconds=600))
    assert taker["kind"] == "taker" and taker["tx"] == H(2001) and taker["url"] == EXPLORER + H(2001)
    assert "same ledger" in v["rules"]["taker"] and "counts in full" in v["rules"]["r21_fix"]
    assert "time quoting, with no gap over 30 s" in v["uptime_rule"] and "exams" in v["rules"]["end"]


def test_fills_latest_first_with_both_effects(tmp_path):
    v = live_view(*build(tmp_path), NOW)
    f = v["fills"]
    assert [x["tx"] for x in f] == [H(2002), H(2001), H(1003), H(1002), H(1001)]
    by = {x["order"]: x for x in f}
    assert by["C"]["double_offer"] and not by["C"]["refill"]
    assert by["B"]["refill"] and not by["B"]["double_offer"]
    assert not by["A"]["refill"] and not by["A"]["double_offer"] and not by["E"]["refill"]
    assert by["D"]["role"] == "taker" and D(by["D"]["price"]) == D("1.1") and by["D"]["url"].startswith("https://livenet.xrpl.org/")


def test_pnl_path_runs_from_zero_to_the_whole_account(tmp_path):
    v = live_view(*build(tmp_path), NOW)
    pts = v["path"]
    assert pts[0]["pnl"] == 0 and pts[0]["mid"] == 1 and pts[-1]["pnl"] == pytest.approx(-1.3) and pts[-1]["mid"] == 0.8
    times = [p["t"] for p in pts]
    anchors = {iso(T0), iso(T0 + timedelta(seconds=600)), iso(T1), iso(T1 + timedelta(seconds=3900))}  # the report's marks
    assert anchors <= set(times) and times == sorted(times)
    marks = [t for t in times if t not in anchors]
    assert len({t[:16] for t in marks}) == len(marks)                                          # one a minute
    breaks = [(p["t"], p["session"]) for p in pts if p["gap_before"]]
    assert breaks[0] == (iso(T1), "session 2") and [b[1] for b in breaks] == ["session 2", "session 3"]  # the sessions, nothing else
    assert {p["session"] for p in pts if p["t"] < iso(T1)} == {"session 1"} and not any(p["unmarked_before"] for p in pts)


def test_a_stretch_without_marks_inside_a_session_is_not_a_break(tmp_path):
    report, data = build(tmp_path)
    import sqlite3
    c = sqlite3.connect(data / "p2.sqlite3")
    with c:   # session 2 loses its marks for 80 s, as when the connection or the monitor went down on 8 Nov
        c.execute("DELETE FROM paper_decisions WHERE at > ? AND at < ?", (iso(T1 + timedelta(seconds=100)), iso(T1 + timedelta(seconds=180))))
    c.close()
    pts = live_view(report, data, NOW)["path"]
    (u,) = [p for p in pts if p["unmarked_before"]]
    assert u["session"] == "session 2" and not u["gap_before"] and sum(p["gap_before"] for p in pts) == 2


def test_a_fill_missing_from_the_report_is_flagged(tmp_path):
    v = live_view(*build(tmp_path, drop_fill=True), NOW)
    bad = [c for c in v["checks"] if not c["ok"]]
    assert [c["name"] for c in bad] == ["period 2: fills in the databases match the report's hashes"]
    assert bad[0]["detail"] == "2 in the databases, 1 in the report"


def test_api_reads_and_writes_nothing(tmp_path):
    report, data = build(tmp_path)
    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.glob("*.sqlite3")}
    with TestClient(create_app(settings(tmp_path, report, data), clock=lambda: NOW)) as client:
        for _ in range(2):
            v = client.get("/api/live").json()
            assert v["enabled"] and D(v["headline"]["account_pnl_rlusd"]) == D("-1.3")
        assert client.post("/api/live").status_code == 405
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.glob("*.sqlite3")} == digests
    assert all(not w.exists() or w.stat().st_size == 0 for w in data.glob("*-wal"))


def test_settings_default_the_live_folder_to_the_monitors(monkeypatch, tmp_path):
    monkeypatch.setenv("DEBTLAB_XRPL_DB", str(tmp_path / "x" / "xrpl.sqlite3"))
    monkeypatch.delenv("DEBTLAB_XRPL_LIVE_DIR", raising=False)
    s = Settings.from_env()
    assert s.xrpl_live_dir == tmp_path / "x" and s.live_report_path == ROOT / "results" / "m8_live_report.json"
    monkeypatch.setenv("DEBTLAB_XRPL_LIVE_DIR", str(tmp_path / "y"))
    assert Settings.from_env().xrpl_live_dir == tmp_path / "y"


def test_page_has_the_section_after_rlusd_xrp():
    page = render_page()
    assert page.index('href="#xrpl"') < page.index('href="#live"') < page.index('href="#risk"')
    assert page.index('id="xrpl"') < page.index('id="live"') < page.index('id="risk"')
    assert "MAINNET: real funds, EUR 50 experiment, run ended" in page and 'id="liveBody"' in page
    assert "function liveSection" in page and "api/live" in page


def test_snapshot_export_includes_the_section(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import export_snapshot
    report, data = build(tmp_path)
    with TestClient(create_app(settings(tmp_path, report, data), clock=lambda: NOW)) as client:
        out = export_snapshot.snapshot_data(client)
    assert out["live_detail"]["enabled"] and out["live_detail"]["headline"]["fills"] == 5


REAL = Path("C:/debtlab-data")


@pytest.mark.skipif(not (REAL / "xrpl_mainnet_live_period2.sqlite3").exists(), reason="the real M8 databases are on Ethan's machine")
def test_the_real_run_matches_the_final_report():
    v = live_view(ROOT / "results" / "m8_live_report.json", REAL, NOW)
    assert v["enabled"] and all(c["ok"] for c in v["checks"]), v.get("checks", v.get("reason"))
    h = v["headline"]
    assert round(D(h["account_pnl_rlusd"]), 4) == D("-2.1698") and round(D(h["vs_hold_rlusd"]), 2) == D("-0.18")
    assert round(h["running_h"], 2) == 17.50 and round(h["quoting_h"], 2) == 7.55 and h["fills"] == 674
    c = v["split"]["combined"]
    assert round(D(c["quoting"]), 4) == D("-0.6970") and round(D(c["not_quoting"]), 4) == D("-1.4502")
    assert [e["kind"] for e in v["events"]] == ["breach", "taker", "taker"]
