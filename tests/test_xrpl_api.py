"""GET /api/xrpl and the dashboard's RLUSD/XRP section (M7 step 4): read-only, live market data."""

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from debtlab.api.app import create_app, render_page
from debtlab.audit.db import connect, migrate
from debtlab.config import Settings
from debtlab.xrpl_market import book, fx, load_config, store, verify
from debtlab.xrpl_market.trades import Trade
from debtlab.xrpl_market.view import xrpl_view

CFG = load_config()
RLUSD, ISSUER = CFG["rlusd"]["currency_hex"], CFG["rlusd"]["issuer"]
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
POOL = verify.Amm(1, "rAMM", Decimal(1000), Decimal(1250), Decimal("1.25"), Decimal("0.214"))


def s(n):
    return T0 + timedelta(seconds=n)


def rl(v):
    return {"currency": RLUSD, "issuer": ISSUER, "value": str(v)}


def mkbook(ledger):
    bids = {"ledger_index": ledger, "validated": True,
            "offers": [{"TakerGets": rl(Decimal("1.249") * 100), "TakerPays": str(100 * 1_000_000)}]}
    asks = {"ledger_index": ledger, "validated": True,
            "offers": [{"TakerGets": str(100 * 1_000_000), "TakerPays": rl(Decimal("1.251") * 100)}]}
    return book.parse_book([bids], [asks], RLUSD)


def populate(path, clean_stop=True):
    c = store.open_db(path)
    store.record_fx(c, fx.FxRate(date(2026, 10, 30), Decimal("1.25"), "ECB"), s(0))
    r = store.start_run(c, s(0))
    for i in range(10):
        store.record_snapshot(c, r, "wss://test", 100 + i, s(4 * i), s(4 * i + 1), "deep" if i % 5 == 0 else "top",
                              mkbook(100 + i), POOL)
    store.record_trades(c, [Trade("H" * 64, "OFF1", 105, "book", "sell_xrp", Decimal("1.249"), Decimal(40),
                                  Decimal("49.96"), "rM", "OfferCreate")], s(22))
    store.record_issuer(c, verify.IssuerSettings(ISSUER, 100, "https://ripple.com/",
                                                 {"allowTrustLineClawback": True, "globalFreeze": False}, Decimal(0), None), s(1))
    store.record_network(c, verify.NetworkCosts(100, Decimal(1), Decimal("0.2"), Decimal(10), Decimal(10)), s(1))
    store.open_outage(c, "stale", s(40), "test outage")
    store.record_snapshot(c, r, "wss://test", 110, s(80), s(81), "top", mkbook(110), POOL)
    if clean_stop:
        store.stop_run(c, r, s(82), "stopped by user")
    c.close()


def settings(tmp_path, xrpl_db):
    return Settings(db_path=tmp_path / "t.sqlite3", data_mode="offline", max_curve_age_hours=96,
                    settlement_date=date(2026, 11, 1),
                    instruments_file=Path(__file__).parent.parent / "config" / "instruments.json",
                    host="127.0.0.1", port=8000, xrpl_db_path=xrpl_db)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_no_database_is_disabled_and_nothing_is_created(tmp_path):
    db = tmp_path / "xrpl.sqlite3"
    with TestClient(create_app(settings(tmp_path, db), clock=lambda: s(90))) as client:
        v = client.get("/api/xrpl").json()
    assert v["enabled"] is False and "scripts/xrpl_monitor.py run" in v["reason"]
    assert v["label"] == "LIVE MARKET DATA, READ-ONLY"
    assert not db.exists()


def test_a_bond_database_is_not_read_as_a_monitor(tmp_path):
    bond = tmp_path / "bond.sqlite3"
    c = connect(bond)
    migrate(c)
    c.close()
    v = xrpl_view(bond, s(0))
    assert v["enabled"] is False


def test_populated_view(tmp_path):
    db = tmp_path / "xrpl.sqlite3"
    populate(db)
    with TestClient(create_app(settings(tmp_path, db), clock=lambda: s(90))) as client:
        v = client.get("/api/xrpl").json()
    assert v["enabled"] and v["monitor"]["state"] == "stopped" and "stopped by user" in v["monitor"]["detail"]
    l = v["latest"]
    assert l["ledger_index"] == 110 and l["valid"] and l["fresh"] and l["age_s"] == 9.0
    assert (Decimal(l["best_bid"]), Decimal(l["best_ask"]), Decimal(l["spread_bp"])) == (Decimal("1.249"), Decimal("1.251"), 16)
    assert l["levels"]["bids"][0] == ["1.249", "100"] and Decimal(l["amm"]["price"]) == Decimal("1.25")
    assert v["latest_deep"]["ledger_index"] == 105 and len(v["latest_deep"]["depth"]) == 4
    assert v["fx"]["usd_per_eur"] == "1.25" and "1 RLUSD = 1 USD" in v["fx"]["assumption"]
    c = v["decision_c"]["all"]
    assert c["ledgers_read"] == 11 and c["both_sides_share"] == 1.0 and Decimal(c["median_spread_bp"]) == 16
    assert Decimal(c["median_bid_eur_50bp"]) == Decimal("124.9") / Decimal("1.25")
    assert [x["pass"] for x in c["criteria"]] == [True, True, False]       # under EUR 100 a side
    assert v["trades"]["recent"][0]["taker_side"] == "sell_xrp" and v["trades"]["last_24h"][0]["trades"] == 1
    (o,) = v["outages"]
    assert o["kind"] == "stale" and not o["open"] and o["seconds"] == 41.0
    assert v["issuer"]["changes_seen"] == 0 and v["network"]["reserve_base_xrp"] == "1"
    assert 0 < v["uptime"]["all"]["share"] < 1


def test_a_crashed_monitor_reads_as_not_running(tmp_path):
    db = tmp_path / "xrpl.sqlite3"
    populate(db, clean_stop=False)
    assert xrpl_view(db, s(85))["monitor"]["state"] == "running"         # heartbeat 4 s old
    v = xrpl_view(db, s(200))
    assert v["monitor"]["state"] == "not running" and "without a clean stop" in v["monitor"]["detail"]
    assert not v["latest"]["fresh"] and v["latest"]["age_s"] == 119.0


def test_reading_writes_nothing(tmp_path):
    db = tmp_path / "xrpl.sqlite3"
    populate(db)
    before = digest(db)
    with TestClient(create_app(settings(tmp_path, db), clock=lambda: s(90))) as client:
        for _ in range(3):
            assert client.get("/api/xrpl").status_code == 200
        assert client.post("/api/xrpl").status_code == 405
    assert digest(db) == before
    # WAL mode: SQLite gives even a read-only reader a -shm index and an empty -wal; no data goes in.
    wal = db.with_name(db.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_page_has_the_section():
    page = render_page()
    assert 'href="#xrpl"' in page and 'id="xrBody"' in page and "Live market data, read-only." in page
    assert "function xrplSection" in page and "api/xrpl" in page


def test_snapshot_export_includes_the_section(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent.parent / "scripts"))
    import export_snapshot
    db = tmp_path / "xrpl.sqlite3"
    populate(db)
    with TestClient(create_app(settings(tmp_path, db), clock=lambda: s(90))) as client:
        data = export_snapshot.snapshot_data(client)
    assert data["xrpl_detail"]["enabled"] and data["xrpl_detail"]["latest"]["ledger_index"] == 110
