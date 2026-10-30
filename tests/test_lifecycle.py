"""Lifecycle in the scenario database: simulation date, record dates and idempotent coupon payments."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from debtlab import ledger, lifecycle
from debtlab.audit.db import connect, migrate, recent_events, set_db_role
from debtlab.instruments import load_instruments
from debtlab.ledger import LedgerError
from debtlab.lifecycle import LifecycleError
from debtlab.marketdata import adapters
from debtlab.money import D, q
from debtlab.pricing.bond import FixedRateBond
from debtlab.pricing.engine import dirty_price_from_curve
from debtlab.scenario import create_scenario_db
from test_marketdata_audit_api import settings

ROOT = Path(__file__).resolve().parent.parent
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.json")
BONDS = [i for i in INSTRUMENTS if isinstance(i, FixedRateBond)]
SHORT = next(b for b in BONDS if b.id == "SHORT-USD-2029")
CENTURY = next(b for b in BONDS if b.id == "CENTURY-GBP-2126")
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
AUG_2026 = SHORT.periods()[0]  # pays 17 Aug 2026, already due on 30 Oct 2026


@pytest.fixture
def scenario_path(tmp_path):
    path = tmp_path / "scenario.sqlite3"
    create_scenario_db(path, settings(tmp_path), now=NOW)
    return path


@pytest.fixture
def conn(scenario_path):
    c = connect(scenario_path)
    yield c
    c.close()


def entitlements(conn, pid):
    return {r["holder"]: dict(r) for r in conn.execute("SELECT * FROM entitlements WHERE payment_id = ?", (pid,))}


def ledger_state(conn):
    return (conn.execute("SELECT COUNT(*) FROM ledger_txns").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            [dict(r) for r in conn.execute("SELECT * FROM entitlements ORDER BY payment_id, holder")],
            [dict(r) for r in conn.execute("SELECT * FROM payment_events ORDER BY id")])


# ---------------------------------------------------------------- guard and simulation date


def test_lifecycle_refused_on_main_database(tmp_path):
    c = connect(tmp_path / "main.sqlite3")
    migrate(c)
    set_db_role(c, "main")
    ledger.seed(c, ledger.load_holders_config(ROOT / "config" / "holders.json"), INSTRUMENTS)
    for action in (lambda: lifecycle.start_simulation(c, date(2026, 10, 30)),
                   lambda: lifecycle.advance_simulation(c, date(2027, 1, 1)),
                   lambda: lifecycle.pay_due(c, BONDS),
                   lambda: lifecycle.schedule_coupon(c, SHORT, AUG_2026)):
        with pytest.raises(LifecycleError, match="only in a scenario database"):
            action()
    assert c.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0] == 0
    assert ledger.balance(c, "alder", "USD") == 0


def test_lifecycle_refused_on_database_older_than_roles(tmp_path):
    c = connect(tmp_path / "old.sqlite3")
    c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")  # e.g. a main database from M2
    with pytest.raises(LifecycleError, match="this one is unmarked"):
        lifecycle.require_scenario(c)
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'db_meta'").fetchone() is None  # untouched


def test_simulation_date_starts_today_and_only_moves_forward(conn):
    assert lifecycle.simulation_date(conn) == date(2026, 10, 30)
    with pytest.raises(LifecycleError, match="already set"):
        lifecycle.start_simulation(conn, date(2026, 1, 1))
    with pytest.raises(LifecycleError, match="only moves forward"):
        lifecycle.advance_simulation(conn, date(2026, 10, 29))
    assert lifecycle.advance_simulation(conn, date(2027, 1, 4)) == date(2026, 10, 30)
    assert lifecycle.simulation_date(conn) == date(2027, 1, 4)
    lifecycle.advance_simulation(conn, date(2027, 1, 4))  # same date: no change, no event
    assert len(recent_events(conn, kind="simulation_advanced")) == 1


def test_coupon_not_payable_before_its_date(conn):
    feb_2027 = SHORT.periods()[1]
    with pytest.raises(LifecycleError, match="isn't due until 2027-02-17"):
        lifecycle.pay_coupon(conn, SHORT, feb_2027)


# ---------------------------------------------------------------- record dates


def test_record_date_rules():
    # Short USD bond: 15 calendar days before payment.
    assert SHORT.record_date(date(2026, 8, 17)) == date(2026, 8, 2)  # a Sunday is fine for calendar days
    # Century GBP bond: 1 business day before. 30 Mar 2027 is a Tuesday; 29 Mar 2027 a Monday.
    assert CENTURY.record_date(date(2027, 3, 30)) == date(2027, 3, 29)
    assert CENTURY.record_date(date(2027, 3, 29)) == date(2027, 3, 26)  # Monday -> previous Friday


def test_bad_record_date_rule_rejected():
    with pytest.raises(ValueError, match="record date"):
        FixedRateBond(id="X", name="X", currency="USD", face=D(100), coupon_rate=D("0.05"), frequency=2,
                      issue_date=date(2026, 1, 1), maturity_date=date(2028, 1, 1), day_count="30/360",
                      benchmark="USD", credit_spread=D(0), record_day_basis="weekly")


# ---------------------------------------------------------------- paying a coupon


def test_first_coupon_pays_each_holder_once(conn):
    [r] = lifecycle.pay_due(conn, BONDS)  # on 30 Oct 2026 only the 17 Aug coupon is due
    assert r.payment_id == "coupon:SHORT-USD-2029:2026-08-17"
    # 4.5% semi-annual = 2.25 per 100: Alder's $4m earns $90,000; all four are owed $225,000.
    assert r.total_due == D("225000") and r.total_paid == D("202500")
    assert r.status == "partly_paid" and r.unpaid == ["dana (payments blocked (compliance hold))"]
    ent = entitlements(conn, r.payment_id)
    assert {h: D(e["amount_due"]) for h, e in ent.items()} == {
        "alder": D(90000), "birch": D(67500), "cedar": D(45000), "dana": D(22500)}
    assert ent["dana"]["status"] == "unpaid" and D(ent["dana"]["amount_paid"]) == 0 and ent["dana"]["txn_id"] is None
    assert ent["alder"]["txn_id"] == "coupon:SHORT-USD-2029:2026-08-17:alder"
    # The ledger: holders' cash in, issuer's cash out, nothing created.
    assert ledger.balance(conn, "alder", "USD") == D(90000)
    assert ledger.balance(conn, "dana", "USD") == 0
    assert ledger.balance(conn, "issuer:SHORT-USD-2029", "USD") == D(-202500)
    assert ledger.check_integrity(conn) == []
    event = conn.execute("SELECT * FROM payment_events").fetchone()
    assert (event["record_date"], event["amount_per_100"], event["rounding_residue"]) == ("2026-08-02", "2.25", "0")


def test_crash_part_way_is_finished_by_rerun_after_restart(scenario_path):
    """Fail after holder 2 of 4, reopen the database, run again: nobody is paid twice."""
    conn = connect(scenario_path)
    paid = []

    def crash_after_two(holder):
        paid.append(holder)
        if len(paid) == 2:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        lifecycle.pay_due(conn, BONDS, after_each=crash_after_two)
    assert paid == ["alder", "birch"]
    assert conn.execute("SELECT status FROM payment_events").fetchone()[0] == "in_progress"
    conn.close()

    conn = connect(scenario_path)  # "restart"
    assert ledger.balance(conn, "alder", "USD") == D(90000)  # holders 1-2 survived the crash
    assert ledger.balance(conn, "cedar", "USD") == 0
    paid.clear()
    [r] = lifecycle.pay_due(conn, BONDS, after_each=paid.append)
    assert paid == ["cedar"]  # only what was still due; dana is blocked, not due
    assert r.status == "partly_paid" and r.total_paid == D(202500) and r.paid_now == 1
    assert {h: ledger.balance(conn, h, "USD") for h in ("alder", "birch", "cedar", "dana")} == {
        "alder": D(90000), "birch": D(67500), "cedar": D(45000), "dana": D(0)}
    assert conn.execute("SELECT COUNT(*) FROM ledger_txns WHERE kind = 'coupon'").fetchone()[0] == 3

    before = ledger_state(conn)
    [r] = lifecycle.pay_due(conn, BONDS)  # third run: nothing changes
    assert r.paid_now == 0 and ledger_state(conn) == before
    assert ledger.check_integrity(conn) == []
    conn.close()


def test_same_database_and_date_give_the_same_result(tmp_path):
    results = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.sqlite3"
        create_scenario_db(path, settings(tmp_path), now=NOW)
        c = connect(path)
        lifecycle.advance_simulation(c, date(2027, 3, 31))
        lifecycle.pay_due(c, BONDS)
        results.append(([{k: v for k, v in e.items() if k not in ("created_at", "updated_at")}
                         for e in lifecycle.payments(c)], ledger.balances(c)))
        c.close()
    assert results[0] == results[1]


def test_transfer_after_record_date_does_not_change_who_is_paid(conn):
    # Alder sells $1m to Birch on 10 Aug, after the 2 Aug record date but before the 17 Aug payment.
    ledger.transfer(conn, "t-aug", "alder", "birch", SHORT.id, D(1_000_000), date(2026, 8, 10))
    [r] = lifecycle.pay_due(conn, BONDS)
    ent = entitlements(conn, r.payment_id)
    assert D(ent["alder"]["amount_due"]) == D(90000)  # still on $4m: the holder of record
    assert D(ent["birch"]["amount_due"]) == D(67500)


def test_record_date_closes_the_books(conn):
    lifecycle.schedule_coupon(conn, SHORT, AUG_2026)
    with pytest.raises(LedgerError, match="fixed up to its record date 2026-08-02"):
        ledger.transfer(conn, "t-late", "alder", "birch", SHORT.id, D(1), date(2026, 8, 1))
    # Other instruments, and later dates, are unaffected.
    ledger.transfer(conn, "t-ok", "alder", "birch", SHORT.id, D(1), date(2026, 8, 3))
    ledger.transfer(conn, "t-fund", "alder", "birch", "PCF-USD-SIM", D(1), date(2026, 8, 1))


def test_advancing_pays_every_coupon_in_date_order(conn):
    lifecycle.advance_simulation(conn, date(2027, 3, 30))
    results = lifecycle.pay_due(conn, BONDS)
    assert [r.payment_id for r in results] == [
        "coupon:SHORT-USD-2029:2026-08-17", "coupon:SHORT-USD-2029:2027-02-17",
        "coupon:CENTURY-GBP-2126:2027-03-30"]
    # Century: 6% annual on a regular first period = 6.00 per 100; Dana holds none.
    century = results[-1]
    assert century.status == "paid" and century.total_paid == D(300000)
    assert ledger.balance(conn, "alder", "GBP") == D(120000)
    assert ledger.balance(conn, "alder", "USD") == D(180000)
    assert ledger.check_integrity(conn) == []


def test_rounding_to_cents_records_the_residue(conn):
    # Odd holdings: 1/3 of a cent's worth of difference has to land somewhere.
    ledger.transfer(conn, "t1", "alder", "birch", SHORT.id, D("0.3333"), date(2026, 7, 1))
    [r] = lifecycle.pay_due(conn, BONDS)
    ent = entitlements(conn, r.payment_id)
    # Alder 3,999,999.6667 x 0.0225 = 89,999.99250075 -> 89,999.99; Birch 3,000,000.3333 -> 67,500.01
    assert D(ent["alder"]["amount_due"]) == D("89999.99")
    assert D(ent["birch"]["amount_due"]) == D("67500.01")
    event = conn.execute("SELECT total_due, rounding_residue FROM payment_events").fetchone()
    exact = D(225000)  # units are conserved, so the exact total is unchanged
    assert D(event["total_due"]) + D(event["rounding_residue"]) == exact
    assert D(event["rounding_residue"]) == 0  # here the two roundings cancel
    assert ledger.check_integrity(conn) == []


def test_lifecycle_is_audited(conn):
    lifecycle.pay_due(conn, BONDS)
    kinds = [e["kind"] for e in recent_events(conn, limit=20)]
    assert kinds.count("coupon_scheduled") == 1 and kinds.count("coupon_run_finished") == 1
    assert sum(1 for e in recent_events(conn, kind="ledger_posted") if e["payload"]["kind"] == "coupon") == 3


# ---------------------------------------------------------------- maturity

AFTER_MATURITY = date(2029, 2, 20)  # 17 Feb 2029 is a Saturday, so the last payments roll to Monday 19 Feb


def short_payments(conn):
    return [e for e in lifecycle.payments(conn) if e["instrument_id"] == SHORT.id]


def test_maturity_pays_principal_once_and_retires_units(conn):
    assert lifecycle.principal_payment(SHORT).pay_date == date(2029, 2, 19)
    lifecycle.advance_simulation(conn, AFTER_MATURITY)
    results = lifecycle.pay_due(conn, BONDS)
    short = [r for r in results if SHORT.id in r.payment_id]
    assert [r.payment_id for r in short][-2:] == [  # the final coupon, then principal
        "coupon:SHORT-USD-2029:2029-02-19", "principal:SHORT-USD-2029:2029-02-19"]
    assert len(short) == 7  # six coupons and the principal
    principal = short[-1]
    assert principal.status == "partly_paid"
    assert (principal.total_due, principal.total_paid) == (D(10_000_000), D(9_000_000))
    # Paid holders' units went back to the issuer, with their cash, in one transaction each.
    assert ledger.holdings(conn, SHORT.id) == {"dana": D(1_000_000)}
    assert ledger.balance(conn, "issuer:SHORT-USD-2029", SHORT.id) == D(-1_000_000)
    assert ledger.balance(conn, "alder", "USD") == D(4_000_000) + 6 * D(90_000)
    assert ledger.balance(conn, "issuer:SHORT-USD-2029", "USD") == -(D(9_000_000) + 6 * D(202_500))
    redemption = conn.execute("SELECT owner, asset, amount FROM ledger_entries"
                              " WHERE txn_id = 'principal:SHORT-USD-2029:2029-02-19:alder'").fetchall()
    assert sorted((r["owner"], r["asset"], r["amount"]) for r in redemption) == [
        ("alder", "SHORT-USD-2029", "-4000000"), ("alder", "USD", "4000000"),
        ("issuer:SHORT-USD-2029", "SHORT-USD-2029", "4000000"), ("issuer:SHORT-USD-2029", "USD", "-4000000")]
    # Status, and what is still owed: Dana's $1m principal and six $22,500 coupons.
    assert lifecycle.instrument_status(conn, SHORT.id)["status"] == "matured"
    assert lifecycle.instrument_status(conn, SHORT.id)["since"] == "2029-02-19"
    owed = lifecycle.unpaid(conn, SHORT.id)
    assert {u["holder"] for u in owed} == {"dana"} and len(owed) == 7
    assert sum(D(u["amount_due"]) for u in owed) == D(1_135_000)
    # The century bond carries on.
    assert lifecycle.instrument_status(conn, CENTURY.id)["status"] == "performing"
    assert ledger.check_integrity(conn) == []
    [matured] = recent_events(conn, kind="instrument_matured")
    assert matured["payload"]["units_outstanding"] == "1000000"

    before = ledger_state(conn)
    assert all(r.paid_now == 0 for r in lifecycle.pay_due(conn, BONDS))  # again: nothing changes
    assert ledger_state(conn) == before


def test_crash_during_principal_is_finished_by_rerun(scenario_path):
    conn = connect(scenario_path)
    lifecycle.advance_simulation(conn, date(2029, 2, 18))
    lifecycle.pay_due(conn, BONDS)  # every coupon before maturity
    lifecycle.advance_simulation(conn, AFTER_MATURITY)
    calls = []

    def crash(holder):
        calls.append(holder)
        if len(calls) == 5:  # final coupon: alder, birch, cedar; principal: alder, birch, then crash
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        lifecycle.pay_due(conn, BONDS, after_each=crash)
    assert lifecycle.instrument_status(conn, SHORT.id)["status"] == "performing"  # not matured half-way
    conn.close()

    conn = connect(scenario_path)
    calls.clear()
    lifecycle.pay_due(conn, BONDS, after_each=calls.append)
    assert calls == ["cedar"]
    assert ledger.holdings(conn, SHORT.id) == {"dana": D(1_000_000)}
    assert ledger.balance(conn, "birch", "USD") == D(3_000_000) + 6 * D(67_500)  # principal once
    assert lifecycle.instrument_status(conn, SHORT.id)["status"] == "matured"
    assert len(recent_events(conn, kind="instrument_matured")) == 1
    assert ledger.check_integrity(conn) == []
    conn.close()


def test_matured_units_can_only_be_redeemed(conn):
    lifecycle.advance_simulation(conn, AFTER_MATURITY)
    lifecycle.pay_due(conn, BONDS)
    with pytest.raises(LedgerError, match="reached its final payment"):
        ledger.transfer(conn, "t-after", "dana", "alder", SHORT.id, D(1), date(2029, 3, 1))


def test_units_moved_after_final_record_date_block_redemption(conn):
    # Final record date is 2 Feb 2029 (15 days before the scheduled 17 Feb). A sale on 10 Feb leaves Alder unable to hand back the units it is paid for.
    ledger.transfer(conn, "t-feb", "alder", "birch", SHORT.id, D(1_000_000), date(2029, 2, 10))
    lifecycle.advance_simulation(conn, AFTER_MATURITY)
    with pytest.raises(LifecycleError, match="moved after its final record date 2029-02-02"):
        lifecycle.pay_due(conn, BONDS)
    assert conn.execute("SELECT COUNT(*) FROM payment_events WHERE kind = 'principal'").fetchone()[0] == 0
    assert lifecycle.instrument_status(conn, SHORT.id)["status"] == "performing"


def test_record_date_counts_back_from_the_scheduled_date():
    # 17 Feb 2029 is a Saturday: paid Monday 19 Feb, but the record date is 15 days before the 17th.
    principal = lifecycle.principal_payment(SHORT)
    assert (principal.scheduled_date, principal.pay_date, principal.record_date) == (
        date(2029, 2, 17), date(2029, 2, 19), date(2029, 2, 2))
    final = lifecycle.coupon_payment(SHORT, SHORT.periods()[-1])
    assert final.record_date == date(2029, 2, 2)


# ---------------------------------------------------------------- default scenario

MISSED = date(2027, 2, 17)  # the first coupon after the trigger on 30 Oct 2026
DECLARED = date(2027, 3, 19)  # 30 days later
RECOVERY_PAID = date(2028, 3, 20)  # 12 months after declaration (19 Mar 2028, a Sunday) rolled to Monday
USD_CURVE = adapters.illustrative_curves(NOW)["USD"].curve


def defaulted(conn, to):
    lifecycle.pay_due(conn, BONDS)  # the August 2026 coupon, paid normally
    lifecycle.trigger_default(conn, SHORT)
    lifecycle.advance_simulation(conn, to)
    return lifecycle.pay_due(conn, BONDS)


def test_trigger_picks_the_next_coupon_and_changes_nothing_yet(conn):
    lifecycle.pay_due(conn, BONDS)
    missed = lifecycle.trigger_default(conn, SHORT)
    assert missed.id == "coupon:SHORT-USD-2029:2027-02-17"
    assert lifecycle.instrument_status(conn, SHORT.id)["status"] == "performing"
    assert lifecycle.scenario_price(conn, SHORT, USD_CURVE, date(2026, 10, 30))["basis"] == "curve + assumed spread"
    with pytest.raises(LifecycleError, match="already set to default"):
        lifecycle.trigger_default(conn, SHORT)


def test_missed_coupon_pays_nobody_and_starts_grace(conn):
    results = defaulted(conn, MISSED)
    missed = next(r for r in results if r.payment_id == "coupon:SHORT-USD-2029:2027-02-17")
    assert missed.status == "missed" and missed.total_paid == 0 and missed.total_due == D(225000)
    ent = entitlements(conn, missed.payment_id)
    assert {h: e["status"] for h, e in ent.items()} == dict.fromkeys(("alder", "birch", "cedar", "dana"), "unpaid")
    assert ent["alder"]["reason"] == "issuer missed payment"
    assert ent["dana"]["reason"].startswith("payments blocked")  # already on hold; still owed
    assert ledger.balance(conn, "alder", "USD") == D(90000)  # only the August coupon
    st = lifecycle.instrument_status(conn, SHORT.id)
    assert (st["status"], st["since"]) == ("grace_period", "2027-02-17")
    with pytest.raises(LifecycleError, match="missed payment"):
        lifecycle.pay_coupon(conn, SHORT, SHORT.periods()[1])  # nobody can pay it by another route


def test_default_declared_after_grace_and_later_coupons_not_paid(conn):
    defaulted(conn, date(2027, 3, 18))
    assert lifecycle.instrument_status(conn, SHORT.id)["status"] == "grace_period"  # day 29
    lifecycle.advance_simulation(conn, date(2027, 9, 1))
    lifecycle.pay_due(conn, BONDS)
    st = lifecycle.instrument_status(conn, SHORT.id)
    assert (st["status"], st["since"]) == ("defaulted", "2027-03-19")
    d = lifecycle.default_terms(conn, SHORT.id)
    assert (d["declared_on"], d["recovery_date"]) == (DECLARED, date(2028, 3, 19))
    # No August 2027 coupon for the defaulted bond; the century bond carries on.
    ids = {e["id"] for e in lifecycle.payments(conn)}
    assert "coupon:SHORT-USD-2029:2027-08-17" not in ids
    assert "coupon:CENTURY-GBP-2126:2027-03-30" in ids
    assert len(recent_events(conn, kind="default_declared")) == 1


def test_recovery_pays_40_percent_of_the_claim_and_retires_units(conn):
    defaulted(conn, RECOVERY_PAID)
    rec = lifecycle.recovery_payment(conn, SHORT)
    assert rec.amount_per_100 == D("40.9")  # 40% x (100 + 2.25 missed coupon)
    assert rec.record_date == date(2028, 3, 4)  # 15 days before the scheduled 19 Mar
    ent = entitlements(conn, rec.id)
    assert {h: D(e["amount_due"]) for h, e in ent.items()} == {
        "alder": D(1_636_000), "birch": D(1_227_000), "cedar": D(818_000), "dana": D(409_000)}
    assert ent["dana"]["status"] == "unpaid"
    assert ledger.holdings(conn, SHORT.id) == {"dana": D(1_000_000)}  # paid holders' units retired
    assert ledger.balance(conn, "issuer:SHORT-USD-2029", "USD") == -(D(202_500) + D(3_681_000))
    losses = {x["holder"]: x for x in lifecycle.default_losses(conn, SHORT)}
    assert (losses["alder"]["claim"], losses["alder"]["recovery_paid"], losses["alder"]["loss"],
            losses["alder"]["loss_pct"]) == ("4090000", "1636000", "2454000", "60")
    assert losses["dana"]["recovery_paid"] == "0" and losses["dana"]["status"] == "unpaid"
    assert lifecycle.scenario_price(conn, SHORT, USD_CURVE, RECOVERY_PAID)["basis"] == "none"
    # The missed coupon is part of the claim the recovery settled: written off, not owed on top.
    missed = entitlements(conn, "coupon:SHORT-USD-2029:2027-02-17")
    assert {e["status"] for e in missed.values()} == {"written_off"}
    owed = {(u["payment_id"], u["holder"]): D(u["amount_due"]) for u in lifecycle.unpaid(conn, SHORT.id)}
    assert owed == {("coupon:SHORT-USD-2029:2026-08-17", "dana"): D(22_500),  # compliance hold, before the default
                    ("recovery:SHORT-USD-2029:2028-03-20", "dana"): D(409_000)}
    assert ledger.check_integrity(conn) == []
    before = ledger_state(conn)
    assert all(r.paid_now == 0 for r in lifecycle.pay_due(conn, BONDS))
    assert ledger_state(conn) == before
    assert len(recent_events(conn, kind="recovery_paid")) == 1


def test_claim_travels_with_units_traded_after_the_default(conn):
    defaulted(conn, date(2027, 6, 1))
    # Distressed trading: Birch buys $1m from Alder after the missed coupon's record date.
    ledger.transfer(conn, "t-distressed", "alder", "birch", SHORT.id, D(1_000_000), date(2027, 6, 1))
    lifecycle.advance_simulation(conn, RECOVERY_PAID)
    lifecycle.pay_due(conn, BONDS)
    rec = lifecycle.recovery_payment(conn, SHORT)
    ent = entitlements(conn, rec.id)
    assert D(ent["alder"]["amount_due"]) == D(1_227_000) and D(ent["birch"]["amount_due"]) == D(1_636_000)
    with pytest.raises(LedgerError, match="final payment"):
        ledger.transfer(conn, "t-late", "cedar", "birch", SHORT.id, D(1), date(2028, 4, 1))


def test_crash_during_recovery_is_finished_by_rerun(scenario_path):
    conn = connect(scenario_path)
    defaulted(conn, date(2028, 3, 1))
    lifecycle.advance_simulation(conn, RECOVERY_PAID)
    calls = []

    def crash(holder):
        calls.append(holder)
        if len(calls) == 2:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        lifecycle.pay_due(conn, BONDS, after_each=crash)
    conn.close()
    conn = connect(scenario_path)
    calls.clear()
    lifecycle.pay_due(conn, BONDS, after_each=calls.append)
    assert calls == ["cedar"]
    assert ledger.balance(conn, "alder", "USD") == D(90000) + D(1_636_000)
    assert ledger.check_integrity(conn) == []
    conn.close()


def test_only_a_performing_bond_with_a_coupon_left_can_default(conn):
    lifecycle.advance_simulation(conn, AFTER_MATURITY)
    lifecycle.pay_due(conn, BONDS)
    with pytest.raises(LifecycleError, match="is matured"):
        lifecycle.trigger_default(conn, SHORT)


def test_default_refused_on_main_database(tmp_path):
    c = connect(tmp_path / "main.sqlite3")
    migrate(c)
    set_db_role(c, "main")
    with pytest.raises(LifecycleError, match="only in a scenario database"):
        lifecycle.trigger_default(c, SHORT)


# ---------------------------------------------------------------- pricing through the default


def test_price_trades_flat_from_the_missed_coupon(conn):
    lifecycle.pay_due(conn, BONDS)
    performing = lifecycle.scenario_price(conn, SHORT, USD_CURVE, date(2027, 2, 1))
    assert performing["quoting"] == "normal" and D(performing["accrued"]) > 0
    assert D(performing["dirty"]) == q(dirty_price_from_curve(SHORT, USD_CURVE, date(2027, 2, 1)))

    lifecycle.trigger_default(conn, SHORT)
    lifecycle.advance_simulation(conn, MISSED)
    lifecycle.pay_due(conn, BONDS)
    grace = lifecycle.scenario_price(conn, SHORT, USD_CURVE, MISSED)
    assert grace["flat"] and grace["accrued"] == "0" and grace["clean"] == grace["dirty"]
    assert grace["quoting"] == "restricted" and grace["spread_bp"] == "1000"
    # Remaining payments at curve + 1,000 bp (the engine shifts the bond's own 110 bp by 890), plus 2.25 owed now.
    expected = dirty_price_from_curve(SHORT, USD_CURVE, MISSED, D(890)) + D("2.25")
    assert D(grace["dirty"]) == q(expected)
    assert D(grace["dirty"]) < D(performing["clean"])

    lifecycle.advance_simulation(conn, DECLARED)
    lifecycle.pay_due(conn, BONDS)
    after = lifecycle.scenario_price(conn, SHORT, USD_CURVE, DECLARED)
    assert after["status"] == "defaulted" and after["quoting"] == "halted" and after["clean"] == after["dirty"]
    # Hand check: 40.90 paid 20 Mar 2028, 367 days away, at zero rate + 10%, semi-annual compounding.
    t = D(367) / D(365)
    r = USD_CURVE.zero_rate(t) + D("0.10")
    assert USD_CURVE.compounding_for(2) == 2
    assert D(after["dirty"]) == q(D("40.9") * (1 + r / 2) ** (-2 * t))
    assert D(30) < D(after["dirty"]) < D("40.9")
    assert "40.9 per 100" in after["basis"]
