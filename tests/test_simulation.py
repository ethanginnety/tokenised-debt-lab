"""The simulated market (M4 step 5): calibration profiles, price-sensitive and informed
clients, fills, fees, slippage, the P&L split, markouts, the unwind rule, determinism,
and finishing a run after a crash."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from debtlab import ledger
from debtlab.audit.db import connect, migrate
from debtlab.instruments import load_instruments
from debtlab.marketdata import adapters
from debtlab.marketmaker import controls, daily, execution, pnl, simulation
from debtlab.marketmaker.book import load_book_config, seed_book
from debtlab.marketmaker.simulation import SimulatedCrash, SimulationError, daily_sd_bp, fill_probability
from debtlab.money import D, q
from debtlab.pricing.bond import FixedRateBond

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
CONFIG = ROOT / "config" / "marketmaker.json"
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.json")
CENTURY = next(i for i in INSTRUMENTS if i.id == "CENTURY-GBP-2126")
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CURVES = {
    "GBP": adapters.parse_boe_zip((REAL / "boe_2026-10-30.zip").read_bytes(), NOW),
    "USD": adapters.to_zero_snapshot(adapters.parse_treasury_csv(
        (REAL / "treasury_2026-10-30.csv").read_text(encoding="utf-8"), NOW)),
}
STEPS = 80


def new_db(path, config=CONFIG):
    c = connect(path)
    migrate(c)
    ledger.seed(c, ledger.load_holders_config(ROOT / "config" / "holders.json"), INSTRUMENTS)
    seed_book(c, load_book_config(config), INSTRUMENTS)
    controls.seed_limits(c, config)
    controls.resume(c, "test", "simulate", NOW)
    return c


def simulate(conn, run_id="r1", seed="7", config=CONFIG, crash_at_step=None, profile="normal", **overrides):
    params = simulation.load_sim_params(config, profile, **{"steps": STEPS, **overrides})
    return simulation.run(conn, run_id, seed, INSTRUMENTS, CURVES, NOW.date(), NOW, 96, config, params,
                          crash_at_step=crash_at_step)


def fills(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM mm_fills ORDER BY step, id")]


# ---------------------------------------------------------------- calibration


def test_profiles_and_the_daily_yield_sd_they_imply():
    normal = simulation.load_sim_params(CONFIG)
    stress = simulation.load_sim_params(CONFIG, "stress")
    assert (normal.profile, normal.jump_prob, stress.jump_prob) == ("normal", D("0.002"), D("0.01"))
    assert (normal.jump_bp, normal.vol_bp_per_step) == (D(8), D("0.2"))
    # sqrt(390 x (0.2^2 + p x 8^2)): 8.1 bp normal (about real 30-year gilts), 16.3 bp stress
    assert q(daily_sd_bp(normal), 1) == D("8.1") and q(daily_sd_bp(stress), 1) == D("16.3")
    assert q(daily_sd_bp(simulation.load_sim_params(CONFIG, jump_prob="0")), 2) == D("3.95")  # diffusion only
    assert simulation.SimParams.from_json(stress.to_json()) == stress
    with pytest.raises(SimulationError, match="no simulation profile"):
        simulation.load_sim_params(CONFIG, "calm")


def test_the_simulated_paths_match_the_implied_sd():
    params = simulation.load_sim_params(CONFIG, "stress")
    moves = [float(simulation.benchmark_paths(str(s), params, ["GBP"])["GBP"][-1]) for s in range(400)]
    sd = (sum(m * m for m in moves) / len(moves)) ** 0.5
    assert abs(sd - float(daily_sd_bp(params))) < 2.0  # 400 days: within about 2 standard errors


# ---------------------------------------------------------------- uninformed clients are price-sensitive


def test_fill_probability_decays_with_distance():
    assert fill_probability(D(0), D("0.02")) == 1
    assert q(fill_probability(D(50), D("0.02")), 6) == q(D(-1).exp(), 6)  # e^-1 = 37%
    assert fill_probability(D(-50), D("0.02")) == fill_probability(D(50), D("0.02"))
    probs = [fill_probability(D(d), D("0.1")) for d in (0, 5, 10, 20)]
    assert probs == sorted(probs, reverse=True) and probs[-1] < D("0.14")


def test_a_wider_spread_gets_fewer_uninformed_fills(tmp_path):
    wide = tmp_path / "wide.json"
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["quoting"]["defaults"]["illiquidity_bp"] = "20"
    data["quoting"]["CENTURY-GBP-2126"]["illiquidity_bp"] = "60"
    wide.write_text(json.dumps(data), encoding="utf-8")
    normal_conn, wide_conn = new_db(tmp_path / "n.sqlite3"), new_db(tmp_path / "w.sqlite3", wide)
    simulate(normal_conn, steps=200)
    simulate(wide_conn, config=wide, steps=200)
    for inst in ("CENTURY-GBP-2126", "SHORT-USD-2029"):
        n = [f for f in fills(normal_conn) if f["instrument_id"] == inst]
        w = [f for f in fills(wide_conn) if f["instrument_id"] == inst]
        assert all(f["client"] == "uninformed" for f in n + w)
        assert len(w) < len(n), inst  # same clients arrive; fewer of them deal at the wider quote


# ---------------------------------------------------------------- informed clients


def test_informed_clients_only_take_stale_quotes(tmp_path):
    conn = new_db(tmp_path / "i.sqlite3")
    simulate(conn, informed_arrival_prob="0.3", steps=200, profile="stress")
    informed = [f for f in fills(conn) if f["client"] == "informed"]
    assert informed, "the test seed should produce informed fills"
    for f in informed:  # the bot always trades the wrong way against the true value
        true_value = D(f["size"]) * D(f["true_fair_dirty"]) / 100
        assert D(f["cash"]) > true_value if f["side"] == "buy" else D(f["cash"]) < true_value
    loss = sum(D(f["size"]) * D(f["true_fair_dirty"]) / 100 - D(f["cash"]) if f["side"] == "buy"
               else D(f["cash"]) - D(f["size"]) * D(f["true_fair_dirty"]) / 100 for f in informed)
    assert loss < 0  # measured against true value, informed flow always costs the bot


def test_informed_clients_come_on_top_of_the_same_uninformed_flow(tmp_path):
    plain_conn, mixed_conn = new_db(tmp_path / "u0.sqlite3"), new_db(tmp_path / "u1.sqlite3")
    simulate(plain_conn, steps=200, profile="stress")
    simulate(mixed_conn, steps=200, profile="stress", informed_arrival_prob="0.3")
    first = min(f["step"] for f in fills(mixed_conn) if f["client"] == "informed")
    key = ("step", "instrument_id", "side", "size", "price")
    before = lambda conn: [tuple(f[k] for k in key) for f in fills(conn) if f["step"] < first]  # noqa: E731
    assert before(plain_conn) and before(plain_conn) == before(mixed_conn)  # identical until the first informed fill
    same_step = [f for f in fills(mixed_conn) if f["step"] == first and f["client"] == "uninformed"]
    assert all(f["id"].endswith(":uninformed") for f in same_step)  # both can fill in one step, under their own ids


# ---------------------------------------------------------------- ledger, limits and P&L


def test_fills_are_ledger_trades_with_fees(tmp_path):
    conn = new_db(tmp_path / "l.sqlite3")
    summary = simulate(conn, informed_arrival_prob="0.045")
    rows = fills(conn)
    assert rows and ledger.check_integrity(conn) == []
    for f in rows:
        txn = conn.execute("SELECT kind, memo FROM ledger_txns WHERE id = ?", (f["txn_id"],)).fetchone()
        assert txn["kind"] == "trade" and "SIMULATED" in txn["memo"]
        assert D(f["fee"]) == q(D(f["cash"]) * D("0.5") / 10_000, 2)  # 0.5 bp of the cash
    for inst, s in summary["instruments"].items():
        assert D(s["inventory_end"]) == D(s["inventory_start"]) + D(s["bought"]) - D(s["sold"])
        assert D(0) <= D(s["inventory_end"]) <= D(2_000_000)
    assert summary["label"] == simulation.LABEL


def test_pnl_identity_by_hand():
    # Start: 100 face marked at 100. Buy 100 for 99 cash (+0.1 fee) when fair is 100; end mark 101.
    # Spread 1; MTM: old 100 x 1/100 + new 100 x (101 - 100)/100 = 2; total 1 + 2 - 0.1 = 2.9.
    # Then an unwind: sell 100 for 98 cash when fair is 100: slippage 2.
    buy = pnl.FillForPnl("X", "GBP", "buy", "uninformed", D(100), D(99), D(100), D("0.1"))
    unwind = pnl.FillForPnl("X", "GBP", "sell", "unwind", D(100), D(98), D(100), D(0))
    p = pnl.attribute([buy, unwind], {"X": "GBP"}, {"X": D(100)}, {"X": D(100)}, {"X": D(101)})["GBP"]
    assert (p.spread, p.fees, p.slippage) == (D(1), D("0.1"), D(2))
    assert p.start_mtm == D(1)  # the 100 held from the start, marked up by 1
    assert p.traded_mtm == D(0)  # the buy's +100 x 1/100, less the unwind's -100 x 1/100
    assert p.mtm == D(1) and p.total == D("-0.1")
    from_ledger = pnl.total({"X": "GBP"}, {"GBP": D(0)}, {"GBP": D("-1.1")}, {"X": D(100)}, {"X": D(100)},
                            {"X": D(100)}, {"X": D(101)})
    assert from_ledger == {"GBP": D("-0.1")}  # cash -99 - 0.1 + 98; units 100 x 101 - 100 x 100, /100


def test_pnl_identity_holds_to_the_cent_with_slippage(tmp_path):
    conn = new_db(tmp_path / "p.sqlite3")
    century = next(i for i in INSTRUMENTS if i.id == "CENTURY-GBP-2126")
    ledger.trade(conn, "pre", century, "market-maker", "market", D(300_000), D(90), NOW.date())  # above target
    summary = simulate(conn, informed_arrival_prob="0.045", unwind_at_end=True)
    assert summary["instruments"][CENTURY.id]["fills"]["unwind"] == 1
    assert summary["instruments"][CENTURY.id]["inventory_end"] == "1000000"  # back to target
    for ccy, p in summary["pnl"].items():
        parts = D(p["spread"]) + D(p["start_mtm"]) + D(p["traded_mtm"]) - D(p["fees"]) - D(p["slippage"])
        assert abs(parts - D(p["total_from_ledger"])) < D("0.005"), ccy  # to the cent (in fact exact)
        assert D(p["total"]) == D(p["total_from_ledger"])
        assert D(p["mtm"]) == D(p["start_mtm"]) + D(p["traded_mtm"])
    assert D(summary["pnl"]["GBP"]["slippage"]) > 0
    assert summary["unwind"] == "at the end of the run"


def test_start_mtm_is_the_market_move_on_the_starting_position(tmp_path):
    conn = new_db(tmp_path / "s.sqlite3")
    summary = simulate(conn, informed_arrival_prob="0.045", profile="stress")
    for inst, s in summary["instruments"].items():
        ccy = next(i.currency for i in INSTRUMENTS if i.id == inst)
        moved = D(s["inventory_start"]) * (D(s["fair_end"]) - D(s["fair_start"])) / 100
        assert D(summary["pnl"][ccy]["start_mtm"]) == moved  # one bond per currency here


def test_markouts_by_hand_and_informed_flow_marks_out_badly(tmp_path):
    conn = new_db(tmp_path / "m.sqlite3")
    for ccy in ("GBP", "USD"):  # in stress one jump can trip the loss limit; keep quoting all day
        controls.set_limit(conn, "max_daily_loss", ccy, D(10_000_000), "test", "see the whole day")
    steps = 390
    summary = simulate(conn, informed_arrival_prob="0.075", steps=steps, profile="stress")
    params = simulation.load_sim_params(CONFIG, "stress", steps=steps, informed_arrival_prob="0.075")
    paths = simulation.benchmark_paths("7", params, ["GBP", "USD"])
    value = simulation._Valuer(conn, CURVES, NOW.date())
    bond = {i.id: i for i in INSTRUMENTS}
    for ccy, by in summary["markouts"].items():
        for client, cell in by.items():
            mine = [f for f in fills(conn) if f["currency"] == ccy and f["client"] == client]
            assert cell["fills"] == len(mine)
            for h in simulation.MARKOUT_STEPS:  # by hand: against the bot's fair value h steps later
                total = D(0)
                for f in mine:
                    later = paths[ccy][min(f["step"] + h, steps - 1) - params.lag_steps]
                    worth = D(f["size"]) * sum(value(bond[f["instrument_id"]], later)) / 100
                    total += worth - D(f["cash"]) if f["side"] == "buy" else D(f["cash"]) - worth
                assert D(cell[f"{h}_steps"]) == total, (ccy, client, h)
    informed = [by["informed"] for by in summary["markouts"].values() if "informed" in by]
    assert informed and all(D(c["30_steps"]) < 0 for c in informed)  # adverse selection


# ---------------------------------------------------------------- unwinding: automatic unless killed


def _over_target(conn):
    ledger.trade(conn, "pre", CENTURY, "market-maker", "market", D(300_000), D(90), NOW.date())


def test_no_automatic_unwind_after_a_kill_only_on_command(tmp_path):
    conn = new_db(tmp_path / "u.sqlite3")
    _over_target(conn)
    controls.kill(conn, "Ethan", "stop everything")
    summary = simulate(conn, unwind_at_end=True)
    assert summary["unwind"].startswith("skipped: the bot is killed")
    assert summary["instruments"][CENTURY.id]["inventory_end"] == "1300000" and fills(conn) == []

    with pytest.raises(SimulationError, match="needs a reason"):
        simulation.unwind(conn, "r1", INSTRUMENTS, CURVES, NOW.date(), CONFIG, "Ethan", " ")
    after = simulation.unwind(conn, "r1", INSTRUMENTS, CURVES, NOW.date(), CONFIG, "Ethan", "flatten the book")
    assert controls.state(conn)["state"] == "killed"  # an unwind doesn't revive the bot
    assert after["unwind"] == "on command by Ethan: flatten the book"
    assert after["instruments"][CENTURY.id]["inventory_end"] == "1000000"
    assert [f["client"] for f in fills(conn)] == ["unwind"]
    p = after["pnl"]["GBP"]
    assert D(p["slippage"]) > 0 and D(p["total"]) == D(p["total_from_ledger"])
    assert json.loads(conn.execute("SELECT summary FROM mm_runs").fetchone()[0]) == after
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'simulation_unwound'").fetchone()[0] == 1
    with pytest.raises(SimulationError, match="already been unwound"):
        simulation.unwind(conn, "r1", INSTRUMENTS, CURVES, NOW.date(), CONFIG, "Ethan", "again")


def test_paused_bot_still_unwinds_at_the_end(tmp_path):
    conn = new_db(tmp_path / "pu.sqlite3")
    _over_target(conn)
    controls.pause(conn, "Ethan", "lunch")
    summary = simulate(conn, unwind_at_end=True)
    assert summary["unwind"] == "at the end of the run"
    assert summary["instruments"][CENTURY.id]["inventory_end"] == "1000000"
    assert [f["client"] for f in fills(conn)] == ["unwind"]


def test_unwind_only_the_latest_finished_run(tmp_path):
    conn = new_db(tmp_path / "ul.sqlite3")
    simulate(conn, run_id="a", steps=10)
    simulate(conn, run_id="b", steps=10)
    with pytest.raises(SimulationError, match="isn't the latest run"):
        simulation.unwind(conn, "a", INSTRUMENTS, CURVES, NOW.date(), CONFIG, "Ethan", "old run")
    with pytest.raises(SimulationError, match="no simulation run"):
        simulation.unwind(conn, "zz", INSTRUMENTS, CURVES, NOW.date(), CONFIG, "Ethan", "typo")


# ---------------------------------------------------------------- many seeds


def test_flatten_and_mean_sd(tmp_path):
    conn = new_db(tmp_path / "f.sqlite3")
    flat = simulation.flatten(simulate(conn, informed_arrival_prob="0.075", profile="stress"))
    assert {"GBP start_mtm", "USD traded_mtm", "GBP informed fills", "USD uninformed markout 30 steps",
            "loss limit tripped"} <= set(flat)
    stats = simulation.mean_sd([{"x": 1.0, "y": 2.0}, {"x": 3.0}])
    assert stats["x"] == (2.0, pytest.approx(2 ** 0.5)) and stats["y"][0] == 1.0  # missing counts as 0


def test_daily_loss_limit_pauses_the_bot_mid_run(tmp_path):
    conn = new_db(tmp_path / "d.sqlite3")
    controls.set_limit(conn, "max_daily_loss", "GBP", D(1), "test", "trip it")
    simulate(conn, steps=200)
    st = controls.state(conn)
    assert (st["state"], st["changed_by"]) == ("paused", "risk check") and st["reason"].startswith("daily loss")
    assert conn.execute("SELECT COUNT(*) FROM mm_orders WHERE status = 'open'").fetchone()[0] == 0
    last_order = conn.execute("SELECT MAX(created_at) FROM mm_orders").fetchone()[0]
    assert last_order <= conn.execute("SELECT MAX(changed_at) FROM mm_state_changes").fetchone()[0]


def test_the_loss_limit_sees_the_same_pnl_as_the_run(tmp_path):
    # Inside a run the limit reads the day's P&L from the ledger, not from the simulation:
    # on a fresh day the two agree within a penny at the run's final marks (the run rounds
    # its marks per 100; the quote round that opens the day doesn't).
    conn = new_db(tmp_path / "same.sqlite3")
    summary = simulate(conn, informed_arrival_prob="0.075", profile="stress")
    marks = {i: execution.Mark(next(b.currency for b in INSTRUMENTS if b.id == i), D(s["fair_end"]), D(0), D(0), D(0))
             for i, s in summary["instruments"].items()}
    from_ledger = daily.daily_pnl(conn, marks, NOW)
    assert set(from_ledger) == set(summary["pnl"])
    for ccy, p in summary["pnl"].items():
        assert abs(from_ledger[ccy] - D(p["total_from_ledger"])) < D("0.01"), ccy
    assert fills(conn)  # it traded, so the agreement means something


def test_nothing_fills_while_killed(tmp_path):
    conn = new_db(tmp_path / "k.sqlite3")
    controls.kill(conn, "Ethan", "not today")
    summary = simulate(conn, informed_arrival_prob="0.075")
    assert fills(conn) == [] and all(s["inventory_end"] == "1000000" for s in summary["instruments"].values())


# ---------------------------------------------------------------- determinism and recovery


def test_same_seed_same_run(tmp_path):
    a, b = new_db(tmp_path / "a.sqlite3"), new_db(tmp_path / "b.sqlite3")
    sa, sb = simulate(a, informed_arrival_prob="0.045"), simulate(b, informed_arrival_prob="0.045")
    assert sa == sb and fills(a) == fills(b) and fills(a)
    c = new_db(tmp_path / "c.sqlite3")
    assert simulate(c, seed="8", informed_arrival_prob="0.045") != sa


def test_crash_part_way_is_finished_by_rerunning(tmp_path):
    clean = new_db(tmp_path / "clean.sqlite3")
    expected = simulate(clean, informed_arrival_prob="0.045")

    crashed = new_db(tmp_path / "crash.sqlite3")
    with pytest.raises(SimulatedCrash):
        simulate(crashed, informed_arrival_prob="0.045", crash_at_step=40)
    assert crashed.execute("SELECT last_step, status FROM mm_runs").fetchone()[:] == (39, "running")
    assert all(f["step"] < 40 for f in fills(crashed))  # step 40's work rolled back with the crash
    crashed.close()

    crashed = connect(tmp_path / "crash.sqlite3")  # a restart
    migrate(crashed)
    assert simulate(crashed, informed_arrival_prob="0.045") == expected
    assert fills(crashed) == fills(clean)
    txns = crashed.execute("SELECT COUNT(*) FROM ledger_txns").fetchone()[0]
    assert simulate(crashed, informed_arrival_prob="0.045") == expected  # a finished run changes nothing
    assert crashed.execute("SELECT COUNT(*) FROM ledger_txns").fetchone()[0] == txns


def test_a_run_id_cant_be_reused_with_other_parameters(tmp_path):
    conn = new_db(tmp_path / "r.sqlite3")
    simulate(conn, steps=20)
    with pytest.raises(SimulationError, match="different seed or parameters"):
        simulate(conn, steps=21)


def test_benchmark_path_is_seeded():
    params = simulation.load_sim_params(CONFIG, steps=50)
    a = simulation.benchmark_paths("7", params, ["GBP", "USD"])
    assert a == simulation.benchmark_paths("7", params, ["GBP", "USD"])
    assert a["GBP"][0] == 0 and len(a["GBP"]) == 51 and a["GBP"] != a["USD"]
    assert all(isinstance(x, Decimal) for x in a["GBP"])
