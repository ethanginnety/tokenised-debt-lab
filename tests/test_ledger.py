"""The simulated holder ledger: double entry, balances as of a date, seeding, and the scenario database."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from debtlab import ledger
from debtlab.api.app import create_app
from debtlab.audit.db import atomic, connect, get_db_role, migrate, recent_events, set_db_role
from debtlab.instruments import load_instruments
from debtlab.ledger import Line, LedgerError
from debtlab.money import D
from debtlab.scenario import create_scenario_db
from test_marketdata_audit_api import settings

ROOT = Path(__file__).resolve().parent.parent
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.json")
HOLDERS = ledger.load_holders_config(ROOT / "config" / "holders.json")
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SHORT, CENTURY, FUND = "SHORT-USD-2029", "CENTURY-GBP-2126", "PCF-USD-SIM"


@pytest.fixture
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture
def seeded(conn):
    ledger.seed(conn, HOLDERS, INSTRUMENTS)
    return conn


def two_holders(conn):
    ledger.add_party(conn, "a", "A (test)", "holder")
    ledger.add_party(conn, "b", "B (test)", "holder")
    ledger.add_party(conn, "issuer:X", "Issuer of X", "issuer")


# ---------------------------------------------------------------- double entry


def test_balanced_transaction_posts_once(conn):
    two_holders(conn)
    lines = [Line("issuer:X", "X", D(-100)), Line("a", "X", D(60)), Line("b", "X", D(40))]
    assert ledger.post(conn, "issue:X", "issue", date(2026, 1, 1), lines) is True
    assert ledger.post(conn, "issue:X", "issue", date(2026, 1, 1), lines) is False  # idempotent
    assert ledger.balance(conn, "a", "X") == D(60)
    assert ledger.balance(conn, "issuer:X", "X") == D(-100)  # what the issuer has issued
    assert len(recent_events(conn, kind="ledger_posted")) == 1
    assert ledger.check_integrity(conn) == []


def test_same_id_different_contents_is_refused(conn):
    two_holders(conn)
    ledger.post(conn, "t1", "issue", date(2026, 1, 1), [Line("issuer:X", "X", D(-10)), Line("a", "X", D(10))])
    with pytest.raises(LedgerError, match="different contents"):
        ledger.post(conn, "t1", "issue", date(2026, 1, 1), [Line("issuer:X", "X", D(-10)), Line("b", "X", D(10))])


@pytest.mark.parametrize("lines, message", [
    ([Line("issuer:X", "X", D(-10)), Line("a", "X", D(9))], "does not balance"),
    ([Line("a", "X", D(0)), Line("issuer:X", "X", D(0))], "zero"),
    ([Line("issuer:X", "X", D(-10))], "at least two"),
    ([Line("issuer:X", "USD", D("-0.001")), Line("a", "USD", D("0.001"))], "decimal places"),
    ([Line("issuer:X", "X", D("-0.00001")), Line("a", "X", D("0.00001"))], "decimal places"),
    ([Line("issuer:X", "X", D(-10)), Line("nobody", "X", D(10))], "unknown party"),
    ([Line("a", "X", D(-10)), Line("b", "X", D(10))], "below zero"),  # holders can't go short
])
def test_invalid_transactions_rejected_and_nothing_written(conn, lines, message):
    two_holders(conn)
    with pytest.raises(LedgerError, match=message):
        ledger.post(conn, "bad", "issue", date(2026, 1, 1), lines)
    assert conn.execute("SELECT COUNT(*) FROM ledger_txns").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == 0


def test_atomic_rolls_back_everything_on_error(conn):
    two_holders(conn)
    with pytest.raises(RuntimeError):
        with atomic(conn):
            ledger.post(conn, "t1", "issue", date(2026, 1, 1), [Line("issuer:X", "X", D(-5)), Line("a", "X", D(5))])
            raise RuntimeError("crash after posting")
    assert ledger.balance(conn, "a", "X") == 0
    assert recent_events(conn, kind="ledger_posted") == []


def test_amounts_are_exact_decimals(conn):
    two_holders(conn)
    for i in range(3):
        ledger.post(conn, f"t{i}", "issue", date(2026, 1, 1), [Line("issuer:X", "USD", D("-0.10")), Line("a", "USD", D("0.10"))])
    assert ledger.balance(conn, "a", "USD") == D("0.30")  # a float sum would give 0.30000000000000004


# ---------------------------------------------------------------- seeding and balances


def test_seed_matches_config_and_is_idempotent(seeded):
    assert ledger.seed(seeded, HOLDERS, INSTRUMENTS) == 0  # second run posts nothing
    assert ledger.holdings(seeded, SHORT) == {
        "alder": D(4000000), "birch": D(3000000), "cedar": D(2000000), "dana": D(1000000)}
    assert ledger.holdings(seeded, CENTURY) == {"alder": D(2000000), "birch": D(1500000), "cedar": D(1500000)}
    assert sum(ledger.holdings(seeded, FUND).values()) == D(100000)
    # The issuer's account mirrors what holders own: units are conserved.
    assert ledger.balance(seeded, "issuer:" + SHORT, SHORT) == D(-10000000)
    assert ledger.check_integrity(seeded) == []
    holders = {p["id"]: p for p in ledger.parties(seeded) if p["role"] == "holder"}
    assert set(holders) == {"alder", "birch", "cedar", "dana"}
    assert holders["dana"]["payments_blocked"] is True and holders["alder"]["payments_blocked"] is False
    assert all("(test)" in p["name"] for p in holders.values())


def test_balances_as_of_a_date(seeded):
    assert ledger.holdings(seeded, SHORT, as_of=date(2026, 2, 16)) == {}  # day before issue
    assert ledger.holdings(seeded, SHORT, as_of=date(2026, 2, 17))["alder"] == D(4000000)
    assert ledger.holdings(seeded, CENTURY, as_of=date(2026, 3, 1)) == {}


def test_bond_positions_must_start_on_the_issue_date(conn, tmp_path):
    import json

    data = json.loads((ROOT / "config" / "holders.json").read_text(encoding="utf-8"))
    data["positions"][0]["issued_on"] = "2026-02-18"
    path = tmp_path / "h.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LedgerError, match="issue date"):
        ledger.seed(conn, ledger.load_holders_config(path), INSTRUMENTS)


# ---------------------------------------------------------------- transfers


def test_transfer_moves_units_and_conserves_them(seeded):
    assert ledger.transfer(seeded, "tr1", "alder", "birch", SHORT, D(500000), date(2026, 10, 30)) is True
    assert ledger.transfer(seeded, "tr1", "alder", "birch", SHORT, D(500000), date(2026, 10, 30)) is False
    h = ledger.holdings(seeded, SHORT)
    assert (h["alder"], h["birch"]) == (D(3500000), D(3500000))
    assert sum(h.values()) == D(10000000)
    assert ledger.holdings(seeded, SHORT, as_of=date(2026, 10, 29))["alder"] == D(4000000)  # history kept
    assert ledger.check_integrity(seeded) == []


@pytest.mark.parametrize("args, message", [
    (("alder", "birch", SHORT, D(4000001), date(2026, 10, 30)), "below zero"),
    (("alder", "birch", SHORT, D(1), date(2026, 2, 1)), "before"),  # back-dated before issue
    (("alder", "alder", SHORT, D(1), date(2026, 10, 30)), "itself"),
    (("alder", "issuer:" + SHORT, SHORT, D(1), date(2026, 10, 30)), "holders"),
    (("alder", "birch", SHORT, D(-5), date(2026, 10, 30)), "positive"),
])
def test_bad_transfers_rejected(seeded, args, message):
    with pytest.raises(LedgerError, match=message):
        ledger.transfer(seeded, "bad", *args)


def test_no_back_dating_behind_a_later_movement(seeded):
    ledger.transfer(seeded, "tr1", "alder", "birch", SHORT, D(100), date(2026, 10, 30))
    with pytest.raises(LedgerError, match="before"):
        ledger.transfer(seeded, "tr2", "alder", "cedar", SHORT, D(100), date(2026, 10, 1))


# ---------------------------------------------------------------- database roles and the scenario database


def test_database_role_is_set_once(conn):
    assert get_db_role(conn) is None
    set_db_role(conn, "scenario")
    set_db_role(conn, "scenario")  # same role again is fine
    with pytest.raises(ValueError, match="already"):
        set_db_role(conn, "main")
    with pytest.raises(ValueError):
        set_db_role(connect(":memory:"), "other")


def test_scenario_database_created_fresh_and_old_one_archived(tmp_path):
    path = tmp_path / "scenario.sqlite3"
    s = settings(tmp_path)
    assert create_scenario_db(path, s, NOW) is None  # nothing to archive the first time
    c = connect(path)
    assert get_db_role(c) == "scenario"
    assert ledger.holdings(c, SHORT)["alder"] == D(4000000)
    ledger.transfer(c, "demo", "alder", "birch", SHORT, D(1), date(2026, 10, 30))
    c.close()

    archived = create_scenario_db(path, s, NOW)  # run again: from scratch
    assert archived is not None and archived.exists() and archived.parent.name == "archive"
    c = connect(path)
    assert ledger.holdings(c, SHORT)["alder"] == D(4000000)  # the demo transfer is gone
    assert [e["kind"] for e in recent_events(c, kind="scenario_created")] == ["scenario_created"]
    old = connect(archived)
    assert ledger.holdings(old, SHORT)["alder"] == D(3999999)  # ...but kept in the archive
    old.close()
    c.close()


def test_scenario_script_refuses_to_replace_a_main_database(tmp_path):
    s = settings(tmp_path)
    main = connect(s.db_path)
    migrate(main)
    set_db_role(main, "main")
    main.close()
    with pytest.raises(ValueError, match="not a scenario database"):
        create_scenario_db(s.db_path, s, NOW)
    assert get_db_role(connect(s.db_path)) == "main"


def test_app_marks_and_reports_its_database(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        db = c.get("/api/overview").json()["database"]
        assert db["role"] == "main" and db["path"].endswith("t.sqlite3")
    conn = connect(tmp_path / "t.sqlite3")
    assert ledger.holdings(conn, SHORT)["dana"] == D(1000000)  # main is seeded too, but never advanced

    scen = tmp_path / "scenario.sqlite3"
    create_scenario_db(scen, settings(tmp_path), NOW)
    with TestClient(create_app(settings(tmp_path, db_path=scen))) as c:
        assert c.get("/api/overview").json()["database"]["role"] == "scenario"


def test_scenario_script_leaves_an_unmarked_database_untouched(tmp_path):
    path = tmp_path / "old.sqlite3"
    c = connect(path)
    c.execute("CREATE TABLE keep (x)")
    c.close()
    with pytest.raises(ValueError, match="not a scenario database"):
        create_scenario_db(path, settings(tmp_path), NOW)
    c = connect(path)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"keep"}  # not migrated, not archived
