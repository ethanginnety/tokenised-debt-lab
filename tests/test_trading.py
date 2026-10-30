"""Market maker's book and trades with a cash leg (M4 step 1)."""

import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from debtlab import ledger, lifecycle
from debtlab.api.app import create_app
from debtlab.audit.db import MIGRATIONS_DIR, connect, migrate
from debtlab.instruments import load_instruments
from debtlab.ledger import LedgerError, Line
from debtlab.marketmaker.book import load_book_config, seed_book
from debtlab.money import D, q
from debtlab.pricing.bond import FixedRateBond
from debtlab.scenario import create_scenario_db
from test_marketdata_audit_api import settings

ROOT = Path(__file__).resolve().parent.parent
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.json")
BONDS = [i for i in INSTRUMENTS if isinstance(i, FixedRateBond)]
SHORT = next(b for b in BONDS if b.id == "SHORT-USD-2029")
CENTURY = next(b for b in BONDS if b.id == "CENTURY-GBP-2126")
HOLDERS = ledger.load_holders_config(ROOT / "config" / "holders.json")
BOOK = load_book_config(ROOT / "config" / "marketmaker.json")
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 30)
MM, MARKET, FUNDING = "market-maker", "market", "funding"


@pytest.fixture
def conn():
    c = connect(":memory:")
    migrate(c)
    ledger.seed(c, HOLDERS, INSTRUMENTS)
    seed_book(c, BOOK, INSTRUMENTS)
    return c


# ---------------------------------------------------------------- migration 007


def test_parties_rebuild_keeps_data_and_foreign_keys(tmp_path):
    old = tmp_path / "migrations"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("00[1-6]_*.sql")):
        shutil.copy(f, old / f.name)
    c = connect(tmp_path / "db.sqlite3")
    migrate(c, old)  # a database as it was at the end of M3
    c.execute("INSERT INTO parties(id, name, role, payments_blocked, created_at) VALUES"
              " ('alder', 'Alder (test)', 'holder', 0, 'x'), ('dana', 'Dana (test)', 'holder', 1, 'x'),"
              " ('issuer:X', 'Issuer of X', 'issuer', 0, 'x')")
    c.execute("INSERT INTO ledger_txns(id, kind, effective_date, fingerprint, created_at)"
              " VALUES ('t1', 'issue', '2026-01-01', 'f', 'x')")
    c.execute("INSERT INTO ledger_entries(txn_id, owner, asset, amount) VALUES"
              " ('t1', 'issuer:X', 'X', '-10'), ('t1', 'alder', 'X', '10')")

    assert migrate(c)[0] == "007_market_parties.sql"  # then any later ones
    assert [(p["id"], p["role"], p["payments_blocked"]) for p in ledger.parties(c)] == [
        ("alder", "holder", False), ("dana", "holder", True), ("issuer:X", "issuer", False)]
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # back on after the rebuild
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    ledger.add_party(c, MM, "Market maker (simulated)", "market_maker")  # the new roles are accepted
    with pytest.raises(Exception, match="FOREIGN KEY"):
        c.execute("INSERT INTO ledger_entries(txn_id, owner, asset, amount) VALUES ('t1', 'nobody', 'X', '1')")


# ---------------------------------------------------------------- the starting book


def test_book_is_seeded_once_with_cash_from_funding(conn):
    roles = {p["id"]: p["role"] for p in ledger.parties(conn)}
    assert (roles[MM], roles[MARKET], roles[FUNDING]) == ("market_maker", "market", "funding")
    assert ledger.balance(conn, MM, SHORT.id) == D(1_000_000)
    assert ledger.balance(conn, MARKET, CENTURY.id) == D(2_000_000)
    assert ledger.balance(conn, MM, "GBP") == D(1_500_000)
    assert ledger.balance(conn, FUNDING, "USD") == D(-6_500_000)  # the cash brought in from outside
    assert ledger.balance(conn, ledger.issuer_id(SHORT.id), SHORT.id) == D(-13_000_000)  # 10m + 3m tap
    assert ledger.holdings(conn, SHORT.id)["alder"] == D(4_000_000)  # test holders untouched
    assert seed_book(conn, BOOK, INSTRUMENTS) == 0  # second run posts nothing
    assert ledger.check_integrity(conn) == []


def test_funding_moves_cash_only(conn):
    with pytest.raises(LedgerError, match="funding account; it moves cash only"):
        ledger.post(conn, "bad", "funding", TODAY, [Line(FUNDING, SHORT.id, D(-1)), Line(MM, SHORT.id, D(1))])


def test_app_seeds_the_book_in_main_only(tmp_path):
    with TestClient(create_app(settings(tmp_path))):
        pass
    assert ledger.balance(connect(tmp_path / "t.sqlite3"), MM, SHORT.id) == D(1_000_000)

    plain, with_mm = tmp_path / "plain.sqlite3", tmp_path / "mm.sqlite3"
    create_scenario_db(plain, settings(tmp_path), NOW)
    create_scenario_db(with_mm, settings(tmp_path), NOW, market_maker=True)
    with TestClient(create_app(settings(tmp_path, db_path=plain))):
        pass  # the app doesn't add a book to a scenario database: its M3 figures stay as they were
    assert MM not in {p["id"] for p in ledger.parties(connect(plain))}
    assert ledger.balance(connect(with_mm), MM, SHORT.id) == D(1_000_000)


# ---------------------------------------------------------------- trades


def test_trade_pays_clean_plus_accrued(conn):
    # Short bond, 4.5% semi-annual, 30/360: 17 Aug to 30 Oct is 73 days, so accrued is
    # 2.25 x 73/180 = 0.9125 per 100. $100,000 at 97.25 clean costs 100,000 x 98.1625 / 100.
    s = ledger.trade(conn, "fill-1", SHORT, buyer=MARKET, seller=MM, face=D(100_000),
                     clean_price=D("97.25"), trade_date=TODAY)
    assert (s.posted, s.flat, q(s.accrued_per_100, 10), s.cash) == (True, False, D("0.9125"), D("98162.50"))
    assert ledger.balance(conn, MM, SHORT.id) == D(900_000)
    assert ledger.balance(conn, MM, "USD") == D("1598162.50")
    assert ledger.balance(conn, MARKET, "USD") == D("4901837.50")
    assert ledger.check_integrity(conn) == []


def test_trade_cash_rounds_to_the_cent(conn):
    # Century bond, 6% annual, ACT/ACT (ICMA): 30 Mar to 30 Oct is 214 of 365 days, so accrued
    # is 6 x 214/365 = 3.517808...; £100,000 at 90.62 clean is £94,137.808... -> £94,137.81.
    s = ledger.trade(conn, "fill-2", CENTURY, buyer=MM, seller=MARKET, face=D(100_000),
                     clean_price=D("90.62"), trade_date=TODAY)
    assert q(s.accrued_per_100, 10) == q(D(6) * 214 / 365, 10) and s.cash == D("94137.81")
    assert ledger.balance(conn, MM, "GBP") == D("1405862.19")


def test_trade_is_idempotent_by_id(conn):
    args = dict(buyer=MARKET, seller=MM, face=D(100_000), clean_price=D("97.25"), trade_date=TODAY)
    assert ledger.trade(conn, "fill-1", SHORT, **args).posted is True
    assert ledger.trade(conn, "fill-1", SHORT, **args).posted is False  # a retry books nothing
    assert ledger.balance(conn, MM, SHORT.id) == D(900_000)
    with pytest.raises(LedgerError, match="different contents"):
        ledger.trade(conn, "fill-1", SHORT, **{**args, "clean_price": D("97.26")})


def test_nobody_sells_short_or_overspends(conn):
    with pytest.raises(LedgerError, match="market-maker's SHORT-USD-2029 balance would go below zero"):
        ledger.trade(conn, "f", SHORT, MARKET, MM, D(1_000_001), D(97), TODAY)
    with pytest.raises(LedgerError, match="market-maker's GBP balance would go below zero"):
        ledger.trade(conn, "f", CENTURY, MM, MARKET, D(2_000_000), D(90), TODAY)  # £1.87m > £1.5m cash
    assert ledger.balance(conn, MM, SHORT.id) == D(1_000_000)


def test_trade_refuses_issuers_funding_and_bad_terms(conn):
    for buyer, seller in ((ledger.issuer_id(SHORT.id), MM), (MM, FUNDING)):
        with pytest.raises(LedgerError, match="trades are between"):
            ledger.trade(conn, "f", SHORT, buyer, seller, D(1000), D(97), TODAY)
    with pytest.raises(LedgerError, match="with itself"):
        ledger.trade(conn, "f", SHORT, MM, MM, D(1000), D(97), TODAY)
    with pytest.raises(LedgerError, match="positive"):
        ledger.trade(conn, "f", SHORT, MARKET, MM, D(0), D(97), TODAY)
    with pytest.raises(LedgerError, match="transfers are between holders"):
        ledger.transfer(conn, "t", FUNDING, MM, "USD", D(1), TODAY)


def test_no_trade_after_the_final_record_date(conn):
    with pytest.raises(LedgerError, match="can't move after its final record date 2029-02-02"):
        ledger.trade(conn, "f", SHORT, MARKET, MM, D(1000), D(99), date(2029, 2, 5))


# ---------------------------------------------------------------- trading flat


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / "scenario.sqlite3"
    create_scenario_db(path, settings(tmp_path), NOW, market_maker=True)
    c = connect(path)
    lifecycle.pay_due(c, BONDS)  # August 2026 coupon
    lifecycle.trigger_default(c, SHORT)  # the 17 Feb 2027 coupon will be missed
    yield c
    c.close()


def test_bond_in_grace_period_trades_flat(scenario):
    lifecycle.advance_simulation(scenario, date(2027, 2, 22))
    lifecycle.pay_due(scenario, BONDS)
    assert lifecycle.instrument_status(scenario, SHORT.id)["status"] == "grace_period"
    s = ledger.trade(scenario, "g-1", SHORT, MARKET, MM, D(100_000), D("88.50"), date(2027, 2, 22))
    assert (s.flat, s.accrued_per_100, s.cash) == (True, 0, D(88_500))  # no accrued: clean = dirty
    # The century bond is performing, so on the same day it still pays accrued:
    # 6 x 329/365 (30 Mar 2026 to 22 Feb 2027) = 5.408219... per 100.
    c = ledger.trade(scenario, "g-2", CENTURY, MARKET, MM, D(100_000), D(90), date(2027, 2, 22))
    assert c.flat is False and c.cash == D("95408.22")


def test_defaulted_bond_trades_flat(scenario):
    lifecycle.advance_simulation(scenario, date(2027, 3, 22))
    lifecycle.pay_due(scenario, BONDS)
    assert lifecycle.instrument_status(scenario, SHORT.id)["status"] == "defaulted"
    s = ledger.trade(scenario, "d-1", SHORT, MARKET, MM, D(100_000), D("36.10"), date(2027, 3, 22))
    assert (s.flat, s.cash) == (True, D(36_100))
    assert ledger.check_integrity(scenario) == []
