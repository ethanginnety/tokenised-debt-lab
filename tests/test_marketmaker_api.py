"""The read-only market maker API and dashboard section (M4 step 7). SIMULATED."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from debtlab.api.app import create_app, render_page
from debtlab.audit.db import connect
from debtlab.marketmaker import controls, simulation
from debtlab.money import D
from debtlab.scenario import create_scenario_db
from test_marketdata_audit_api import settings
from test_simulation import CONFIG, CURVES, INSTRUMENTS, NOW

ROOT = Path(__file__).resolve().parent.parent
LATER = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)


def counts(path):
    c = connect(path)
    try:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        c.close()


def simulate_on(path, run_id="api-demo", steps=60):
    """A run on the app's database, as scripts/mm.py run does it, on the real fixture curves."""
    c = connect(path)
    try:
        controls.resume(c, "Ethan", "demo", NOW)
        params = simulation.load_sim_params(CONFIG, "stress", steps=steps, informed_arrival_prob="0.1")
        return simulation.run(c, run_id, "7", INSTRUMENTS, CURVES, NOW.date(), NOW, 96, CONFIG, params)
    finally:
        c.close()


def test_a_fresh_main_database(tmp_path):
    cfg = settings(tmp_path)
    with TestClient(create_app(cfg, clock=lambda: LATER)) as c:
        mm = c.get("/api/marketmaker").json()
    assert mm["enabled"] is True and mm["label"] == "SIMULATED: not evidence of real profitability"
    assert mm["database"]["role"] == "main"
    assert (mm["state"]["state"], mm["state"]["reason"]) == ("paused", "never started")
    assert mm["holdings"]["units"] == {"CENTURY-GBP-2126": "1000000", "SHORT-USD-2029": "1000000"}
    assert mm["holdings"]["cash"] == {"GBP": "1500000", "USD": "1500000"}
    assert mm["day"] == {"date": "2026-10-30", "opened": False, "pnl": []}
    assert len(mm["limits"]) == 14 and {r["changed_by"] for r in mm["limits"]} == {"config"}
    assert mm["runs"] == [] and mm["open_orders"] == [] and mm["integrity"] == []
    assert mm["marks_on_market_data"] is False  # offline mode: illustrative curves, and it says so
    assert "scripts/mm.py" in mm["controls_note"]


def test_reading_it_writes_nothing(tmp_path):
    cfg = settings(tmp_path)
    with TestClient(create_app(cfg, clock=lambda: LATER)) as c:
        c.get("/api/marketmaker")  # after startup's own writes
        before = counts(cfg.db_path)
        for _ in range(3):
            assert c.get("/api/marketmaker").status_code == 200
        assert counts(cfg.db_path) == before


def test_after_a_run_it_shows_the_split_the_day_and_the_quotes(tmp_path):
    cfg = settings(tmp_path)
    with TestClient(create_app(cfg, clock=lambda: LATER)):
        pass  # the app sets up the main database and the book
    summary = simulate_on(cfg.db_path)
    with TestClient(create_app(cfg, clock=lambda: LATER)) as c:
        mm = c.get("/api/marketmaker").json()
    [run] = mm["runs"]
    assert (run["id"], run["status"], run["steps_done"], run["steps"]) == ("api-demo", "finished", 60, 60)
    assert run["summary"]["pnl"] == summary["pnl"]
    for p in run["summary"]["pnl"].values():  # the split adds up to the ledger total, to the cent
        parts = D(p["spread"]) + D(p["start_mtm"]) + D(p["traded_mtm"]) - D(p["fees"]) - D(p["slippage"])
        assert parts == D(p["total"]) == D(p["total_from_ledger"])
    assert mm["day"]["opened"] is True and {r["currency"] for r in mm["day"]["pnl"]} == {"GBP", "USD"}
    gbp = next(r for r in mm["day"]["pnl"] if r["currency"] == "GBP")
    assert gbp["loss_limit"] == "20000" and gbp["limit_used_pct"] is not None
    assert mm["quotes"] and {"status", "reasons", "detail"} <= set(mm["quotes"][0])
    assert mm["state"]["state"] in ("running", "paused")  # paused if the stress run tripped the loss limit


def test_a_scenario_database_without_a_market_maker(tmp_path):
    path = tmp_path / "scenario.sqlite3"
    create_scenario_db(path, settings(tmp_path), now=NOW)
    with TestClient(create_app(settings(tmp_path, db_path=path))) as c:
        mm = c.get("/api/marketmaker").json()
    assert mm["enabled"] is False and "--market-maker" in mm["reason"]
    assert mm["label"].startswith("SIMULATED")


def test_the_page_has_the_section_and_the_snapshot_carries_it(tmp_path):
    page = render_page("{}")
    assert 'id="marketmaker"' in page and 'href="#marketmaker"' in page
    assert "not evidence of real profitability" in page
    spec = importlib.util.spec_from_file_location("export_snapshot", ROOT / "scripts" / "export_snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with TestClient(create_app(settings(tmp_path), clock=lambda: LATER)) as c:
        data = mod.snapshot_data(c)
    assert data["marketmaker_detail"]["enabled"] is True  # a static page can't fetch it


def test_quote_decisions_are_tagged_with_their_run_and_step(tmp_path):
    # Two runs started seconds apart overlap on the simulated clock (as in the demo); each
    # decision still belongs to exactly one, because a run quotes on whole steps from its start.
    cfg = settings(tmp_path)
    with TestClient(create_app(cfg, clock=lambda: LATER)):
        pass
    c = connect(cfg.db_path)
    try:
        controls.resume(c, "Ethan", "demo", NOW)
        for run_id, start in (("first", NOW), ("second", NOW + timedelta(seconds=7))):
            params = simulation.load_sim_params(CONFIG, "normal", steps=30)
            simulation.run(c, run_id, "7", INSTRUMENTS, CURVES, NOW.date(), start, 96, CONFIG, params)
    finally:
        c.close()
    with TestClient(create_app(cfg, clock=lambda: LATER)) as client:
        quotes = client.get("/api/marketmaker").json()["quotes"]
    assert [(q["run_id"], q["step"]) for q in quotes[:3]] == [("second", 20)] * 3  # newest first
    assert {q["run_id"] for q in quotes} == {"first", "second"}
    assert all(q["step"] % 10 == 0 for q in quotes)  # requotes every 10 steps


def test_page_fixes_from_the_browser_check():
    page = render_page("{}")
    assert page.count("Milestone 4 complete") == 1  # once, in the page head
    assert "[hidden] { display: none !important; }" in page  # an empty banner stays hidden
    assert 'id="logOrders"' in page and '"order_placed", "order_cancelled"' in page  # order events toggle
    assert "bot still running at end of run" in page
    assert ".filter(Boolean)" in page  # no stray "null" in the market maker section
