"""The read-only lifecycle API: what the dashboard shows of a scenario database."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from debtlab import lifecycle
from debtlab.api.app import create_app, render_page
from debtlab.audit.db import connect
from debtlab.instruments import load_instruments
from debtlab.marketdata import adapters
from debtlab.money import D
from debtlab.pricing.bond import FixedRateBond
from debtlab.scenario import create_scenario_db
from test_marketdata_audit_api import settings

ROOT = Path(__file__).resolve().parent.parent
BONDS = [i for i in load_instruments(ROOT / "config" / "instruments.json") if isinstance(i, FixedRateBond)]
SHORT = next(b for b in BONDS if b.id == "SHORT-USD-2029")
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def scenario(tmp_path):
    """A fresh scenario database, and settings that point the app at it."""
    path = tmp_path / "scenario.sqlite3"
    create_scenario_db(path, settings(tmp_path), now=NOW)
    return path, settings(tmp_path, db_path=path)


def run(path, *steps):
    """Drive the lifecycle the way scripts/lifecycle.py does, on its own connection."""
    conn = connect(path)
    try:
        for step in steps:
            step(conn)
    finally:
        conn.close()


def advance(to):
    def step(conn):
        lifecycle.advance_simulation(conn, to)
        lifecycle.pay_due(conn, BONDS)
    return step


def trigger(bond):
    return lambda conn: lifecycle.trigger_default(conn, bond)


def tables(path):
    """Every row of every table, to prove a read changed nothing."""
    conn = connect(path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {n: [tuple(r) for r in conn.execute(f"SELECT * FROM {n}")] for n in names}  # noqa: S608
    finally:
        conn.close()


def bond(view, bond_id):
    return next(b for b in view["bonds"] if b["id"] == bond_id)


def test_main_database_says_lifecycle_is_off(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        life = c.get("/api/lifecycle").json()
        assert life["enabled"] is False and life["database"]["role"] == "main"
        assert "scenario database" in life["reason"] and "never advanced" in life["reason"]
        assert "bonds" not in life and "payments" not in life
        assert c.get("/api/overview").json()["lifecycle"] == {"enabled": False, "reason": life["reason"]}


def test_fresh_scenario_shows_performing_bonds_and_no_payments(scenario):
    path, cfg = scenario
    with TestClient(create_app(cfg)) as c:
        life = c.get("/api/lifecycle").json()
        assert life["enabled"] is True and life["simulation_date"] == "2026-10-30"
        assert life["payments"] == [] and life["owed"] == [] and life["integrity"] == []
        short = bond(life, "SHORT-USD-2029")
        assert short["status"] == "performing" and short["default"] is None
        assert short["units_outstanding"] == "10000000"
        assert short["price"]["quoting"] == "normal" and short["price"]["curve"]["kind"] == "illustrative"
        # The scenario price on the simulation date, on the same curve the dashboard uses.
        curve = adapters.illustrative_curves(NOW)["USD"].curve
        conn = connect(path)
        expected = lifecycle.scenario_price(conn, SHORT, curve, date(2026, 10, 30))
        conn.close()
        assert short["price"]["clean"] == expected["clean"]
        assert short["today"]["settle"] == "2026-10-29" and short["today"]["clean"]  # today's valuation beside it
        holders = {h["id"]: h for h in life["holders"]}
        assert set(holders) == {"alder", "birch", "cedar", "dana"}
        assert holders["dana"]["payments_blocked"] is True
        assert holders["alder"]["positions"]["SHORT-USD-2029"] == "4000000"
        assert c.get("/api/overview").json()["lifecycle"] == {
            "enabled": True, "simulation_date": "2026-10-30",
            "statuses": {"CENTURY-GBP-2126": "performing", "SHORT-USD-2029": "performing"},
            "payments": 0, "owed": 0}


def test_reads_change_nothing_and_there_is_no_write_endpoint(scenario):
    path, cfg = scenario
    run(path, advance(date(2026, 11, 2)))  # the August coupon, with Dana unpaid
    with TestClient(create_app(cfg)) as c:
        before = tables(path)
        for _ in range(3):
            assert c.get("/api/lifecycle").status_code == 200
            assert c.get("/api/overview").status_code == 200
            assert c.get("/").status_code == 200
        assert tables(path) == before
        for method in ("post", "put", "patch", "delete"):
            assert getattr(c, method)("/api/lifecycle").status_code == 405


def test_coupon_with_entitlements_and_amount_owed(scenario):
    path, cfg = scenario
    run(path, advance(date(2026, 11, 2)))
    with TestClient(create_app(cfg)) as c:
        life = c.get("/api/lifecycle").json()
        (aug,) = life["payments"]
        assert aug["id"] == "coupon:SHORT-USD-2029:2026-08-17" and aug["status"] == "partly_paid"
        assert aug["record_date"] == "2026-08-02"
        assert (aug["total_due"], aug["total_paid"]) == ("225000", "202500")
        ents = {e["holder"]: e for e in aug["entitlements"]}
        assert ents["alder"]["amount_paid"] == "90000" and ents["alder"]["status"] == "paid"
        assert ents["dana"]["status"] == "unpaid" and "compliance hold" in ents["dana"]["reason"]
        assert life["owed"] == [{"holder": "dana", "currency": "USD", "amount": "22500", "count": 1}]
        assert {h["id"]: h for h in life["holders"]}["alder"]["cash"] == {"USD": "90000"}


def test_api_follows_the_cli_without_a_restart(scenario):
    path, cfg = scenario
    with TestClient(create_app(cfg)) as c:
        assert bond(c.get("/api/lifecycle").json(), "SHORT-USD-2029")["status"] == "performing"
        run(path, trigger(SHORT), advance(date(2027, 2, 17)))  # what `lifecycle.py` does while the app runs
        life = c.get("/api/lifecycle").json()
        assert life["simulation_date"] == "2027-02-17"
        assert bond(life, "SHORT-USD-2029")["status"] == "grace_period"


def test_default_timeline_price_and_losses(scenario):
    path, cfg = scenario
    run(path, trigger(SHORT), advance(date(2027, 2, 17)))
    with TestClient(create_app(cfg)) as c:
        short = bond(c.get("/api/lifecycle").json(), "SHORT-USD-2029")
        assert short["status"] == "grace_period"
        p = short["price"]
        assert p["flat"] is True and p["accrued"] == "0" and p["quoting"] == "restricted"
        assert p["spread_bp"] == "1000"
        d = short["default"]
        assert d["missed_payment_id"] == "coupon:SHORT-USD-2029:2027-02-17"
        assert (d["claim_per_100"], d["recovery_per_100"], d["recovery_rate_pct"]) == ("102.25", "40.9", "40")
        steps = {s["step"]: s for s in d["timeline"]}
        assert [s["step"] for s in d["timeline"]] == ["triggered", "missed", "declared", "recovered"]
        assert steps["missed"]["state"] == "done" and steps["missed"]["date"] == "2027-02-17"
        assert steps["declared"] == {"step": "declared", "date": "2027-03-19", "state": "upcoming",
                                     "label": "Default declared after 30-day grace"}
        assert d["losses"] == []  # nothing recovered yet

        run(path, advance(date(2027, 3, 19)))
        short = bond(c.get("/api/lifecycle").json(), "SHORT-USD-2029")
        assert short["status"] == "defaulted" and short["price"]["quoting"] == "halted"
        assert {s["step"]: s["state"] for s in short["default"]["timeline"]}["declared"] == "done"

        run(path, advance(date(2028, 3, 20)))
        life = c.get("/api/lifecycle").json()
        short = bond(life, "SHORT-USD-2029")
        assert short["price"]["basis"] == "none" and short["units_outstanding"] == "1000000"  # Dana's
        assert {s["step"]: s["state"] for s in short["default"]["timeline"]}["recovered"] == "done"
        losses = {x["holder"]: x for x in short["default"]["losses"]}
        assert (losses["alder"]["claim"], losses["alder"]["recovery_paid"], losses["alder"]["loss_pct"]) == (
            "4090000", "1636000", "60")
        owed = {(o["holder"], o["currency"]): o for o in life["owed"]}
        assert owed[("dana", "USD")]["amount"] == "431500"  # August coupon on hold + her recovery
        written_off = [e for p in life["payments"] if p["status"] == "missed" for e in p["entitlements"]]
        assert {e["status"] for e in written_off} == {"written_off"}


def test_due_but_not_yet_run_is_shown_as_due(scenario):
    path, cfg = scenario
    run(path, trigger(SHORT),
        lambda conn: lifecycle.advance_simulation(conn, date(2027, 2, 17)))  # advanced, but `pay` not run
    with TestClient(create_app(cfg)) as c:
        steps = {s["step"]: s["state"] for s in bond(c.get("/api/lifecycle").json(), "SHORT-USD-2029")
                 ["default"]["timeline"]}
        assert steps["missed"] == "due"


def test_snapshot_export_carries_the_lifecycle(scenario):
    import importlib.util

    spec = importlib.util.spec_from_file_location("export_snapshot", ROOT / "scripts" / "export_snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _, cfg = scenario
    with TestClient(create_app(cfg)) as c:
        data = mod.snapshot_data(c)
        assert data["lifecycle"]["enabled"] is True
        assert data["lifecycle_detail"]["simulation_date"] == "2026-10-30"  # a static page can't fetch it
        assert 'id="lifecycle"' in render_page("{}")


def test_matured_bond_has_nothing_to_price(scenario):
    path, cfg = scenario
    run(path, advance(date(2029, 2, 20)))
    with TestClient(create_app(cfg)) as c:
        short = bond(c.get("/api/lifecycle").json(), "SHORT-USD-2029")
        assert short["status"] == "matured" and short["since"] == "2029-02-19"
        assert short["price"]["basis"] == "none" and "matured" in short["price"]["message"]
        assert D(short["units_outstanding"]) == D(1000000)
