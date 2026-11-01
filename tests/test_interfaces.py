"""Strategy, risk check and venue interfaces (M4 step 7): what a paper or live adapter can
rely on. A recording venue stands in for the XRPL adapters to come."""

import pytest

from debtlab import ledger
from debtlab.audit.db import recent_events
from debtlab.marketmaker import controls, execution, interfaces, simulation
from debtlab.marketmaker.execution import LedgerRiskCheck, place_order
from debtlab.marketmaker.interfaces import RiskCheck, SimulatedVenue, Strategy, Venue, active_venue, use_venue
from debtlab.marketmaker.quoting import QuoteModelStrategy
from debtlab.money import D
from test_controls import CENTURY, MARKS, NOW, ask, bid, open_db
from test_simulation import CONFIG, CURVES, INSTRUMENTS, new_db, simulate

BOND = {i.id: i for i in INSTRUMENTS}


class RecordingVenue:
    name = "recording venue (test)"
    simulated = True

    def __init__(self, refuse=False, fail_cancel=False):
        self.submitted, self.cancelled, self.refuse, self.fail_cancel = [], [], refuse, fail_cancel

    def submit(self, conn, order, now):
        if self.refuse:
            raise RuntimeError("venue down")
        self.submitted.append(order.id)

    def cancel(self, conn, order_ids, reason, now):
        if self.fail_cancel:
            raise RuntimeError("venue down")
        self.cancelled.append((tuple(order_ids), reason))


@pytest.fixture
def running(tmp_path):
    c = open_db(tmp_path / "i.sqlite3")
    controls.resume(c, "test", "go", NOW)
    yield c
    c.close()


def test_the_defaults_implement_the_interfaces():
    assert isinstance(QuoteModelStrategy(CONFIG, 96), Strategy)
    assert isinstance(LedgerRiskCheck(), RiskCheck)
    assert isinstance(SimulatedVenue(), Venue) and SimulatedVenue().simulated
    assert isinstance(active_venue(), SimulatedVenue)  # nothing in M4 talks to a real venue
    with pytest.raises(TypeError, match="doesn't implement Venue"):
        with use_venue(object()):
            pass


def test_only_orders_that_pass_the_risk_check_reach_the_venue(running):
    with use_venue(RecordingVenue()) as v:
        assert place_order(running, bid(1), MARKS, NOW).allowed
        assert place_order(running, bid(1), MARKS, NOW).already_placed  # a retry isn't sent twice
        refused = place_order(running, bid(2, size=250_001), MARKS, NOW)
    assert not refused.allowed and v.submitted == ["bid-1"]
    assert recent_events(running, kind="order_placed")[0]["payload"]["venue"] == "recording venue (test)"
    assert isinstance(active_venue(), SimulatedVenue)  # back to the default afterwards


def test_a_venue_that_refuses_leaves_nothing_behind(running):
    with use_venue(RecordingVenue(refuse=True)):
        with pytest.raises(RuntimeError, match="venue down"):
            place_order(running, bid(1), MARKS, NOW)
    assert execution.open_orders(running) == []
    assert recent_events(running, kind="order_placed") == []


def test_pause_and_the_kill_switch_reach_the_venue(running):
    with use_venue(RecordingVenue()) as v:
        place_order(running, bid(1), MARKS, NOW)
        place_order(running, ask(1), MARKS, NOW)
        controls.kill(running, "Ethan", "stop", NOW)
        assert v.cancelled == [(("ask-1", "bid-1"), "kill switch: stop")]
        assert not place_order(running, bid(2), MARKS, NOW).allowed and v.submitted == ["bid-1", "ask-1"]


def test_a_venue_failing_to_cancel_never_undoes_the_kill(running):
    with use_venue(RecordingVenue(fail_cancel=True)):
        place_order(running, bid(1), MARKS, NOW)
        controls.kill(running, "Ethan", "stop", NOW)
    assert controls.state(running)["state"] == "killed" and execution.open_orders(running) == []
    [failed] = recent_events(running, kind="venue_cancel_failed")
    assert failed["payload"]["orders"] == ["bid-1"] and "venue down" in failed["payload"]["error"]


def test_a_risk_check_can_be_swapped(running):
    class RefuseAll:
        name = "refuse everything (test)"

        def check(self, conn, order, marks, now, pending=(), replacing=frozenset()):
            return ["refused by the test risk check"]

        def after_refusal(self, conn, marks, now):
            return False

    assert isinstance(RefuseAll(), RiskCheck)
    d = place_order(running, bid(1), MARKS, NOW, risk=RefuseAll())
    assert d.reasons == ("refused by the test risk check",) and execution.open_orders(running) == []


def test_a_strategy_can_be_swapped_and_still_passes_the_risk_check(tmp_path):
    class QuoteNothing:
        name = "quote nothing (test)"

        def decide(self, conn, instruments, curves, on, now):
            return []

    conn = new_db(tmp_path / "s.sqlite3")
    params = simulation.load_sim_params(CONFIG, "normal", steps=40)
    s = simulation.run(conn, "quiet", "7", INSTRUMENTS, CURVES, NOW.date(), NOW, 96, CONFIG, params,
                       strategy=QuoteNothing())
    assert conn.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 0
    assert all(sum(x["fills"].values()) == 0 for x in s["instruments"].values())
    # The default strategy, named explicitly, gives exactly the same run as leaving it out.
    a = simulate(new_db(tmp_path / "a.sqlite3"), seed="5")
    b_conn = new_db(tmp_path / "b.sqlite3")
    b = simulation.run(b_conn, "r1", "5", INSTRUMENTS, CURVES, NOW.date(), NOW, 96, CONFIG,
                       simulation.load_sim_params(CONFIG, "normal", steps=80), strategy=QuoteModelStrategy(CONFIG, 96))
    assert a == b


def test_book_fill_is_the_same_for_every_venue(running):
    place_order(running, bid(1), MARKS, NOW)  # 100,000 at 90
    bond = BOND[CENTURY]
    s = execution.book_fill(running, "v-1", bond, "buy", D(90), D(40_000), D("1.80"), "market", "funding",
                            NOW.date(), NOW, order_id="bid-1")
    assert s.cash == ledger.trade_cash(running, bond, D(40_000), D(90), NOW.date())
    [o] = execution.open_orders(running)
    assert (o["filled"], o["remaining"]) == ("40000", D(60_000))
    assert ledger.balance(running, "funding", "GBP") < 0  # it funded the book; the fee came back to it
    assert running.execute("SELECT 1 FROM ledger_txns WHERE id = 'fee:v-1'").fetchone()
    assert ledger.check_integrity(running) == []
    assert interfaces.active_venue().simulated
