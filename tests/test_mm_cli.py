"""scripts/mm.py (M4 step 6): the controls, limits, a run and status, on a main database
set up as the app sets it up, with the real fixture curves stored and a fixed clock."""

import importlib.util
import json
from pathlib import Path

import pytest

from debtlab import ledger
from debtlab.audit.db import connect, migrate, recent_events, save_curve, set_db_role
from debtlab.instruments import load_instruments
from debtlab.marketdata import adapters
from debtlab.marketmaker import controls, execution, simulation
from debtlab.money import D
from test_simulation import CONFIG, CURVES, INSTRUMENTS, NOW

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
spec = importlib.util.spec_from_file_location("mm_cli", ROOT / "scripts" / "mm.py")
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

AT = "2026-10-30T13:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "main.sqlite3"
    c = connect(path)
    migrate(c)
    set_db_role(c, "main")
    ledger.seed(c, ledger.load_holders_config(ROOT / "config" / "holders.json"),
                load_instruments(ROOT / "config" / "instruments.json"))
    save_curve(c, adapters.parse_boe_zip((REAL / "boe_2026-10-30.zip").read_bytes(), NOW))
    save_curve(c, adapters.parse_treasury_csv((REAL / "treasury_2026-10-30.csv").read_text(encoding="utf-8"), NOW))
    c.close()
    return path


def cli(db, *args, at=AT):
    mm.main([*args, "--db", str(db), "--by", "Ethan", "--at", at])


def open_(db):
    return connect(db)


def test_never_started_means_status_shows_paused_and_run_is_refused(db, capsys):
    cli(db, "status")
    out = capsys.readouterr().out
    assert "bot PAUSED (never started)" in out and "max_daily_loss" in out and "not opened yet" in out
    with pytest.raises(SystemExit, match="the bot is paused .*resume it first"):
        cli(db, "run", "--steps", "20")


def test_every_state_change_needs_a_reason_and_is_audited(db, capsys):
    with pytest.raises(SystemExit):
        cli(db, "resume")  # no --reason: argparse refuses
    cli(db, "resume", "--reason", "start")
    cli(db, "kill", "--reason", "looks wrong")
    with pytest.raises(SystemExit, match="refused: the kill switch is on"):
        cli(db, "resume", "--reason", "oops")
    cli(db, "reset", "--reason", "checked the data")
    c = open_(db)
    st = controls.state(c)
    assert (st["state"], st["changed_by"]) == ("paused", "Ethan")  # reset leaves it paused
    kinds = [e["payload"]["to"] for e in reversed(recent_events(c, kind="mm_state_changed"))]
    assert kinds == ["running", "killed", "paused"]
    c.close()
    assert "KILLED" in capsys.readouterr().out


def test_limits_are_shown_and_changed_with_who_and_why(db, capsys):
    cli(db, "limits", "max_daily_loss", "GBP", "15000", "--reason", "tighter")
    out = capsys.readouterr().out
    assert "max_daily_loss GBP: 20000 -> 15000" in out and "by Ethan: tighter" in out
    c = open_(db)
    assert controls.limit(c, "max_daily_loss", "GBP") == D(15_000)
    c.close()
    with pytest.raises(SystemExit, match="refused: unknown limit"):
        cli(db, "limits", "max_fun", "GBP", "1", "--reason", "x")
    with pytest.raises(SystemExit):
        cli(db, "limits", "max_daily_loss", "GBP", "1")  # no reason


def test_a_run_then_status_shows_the_days_pnl_from_the_ledger(db, capsys):
    cli(db, "resume", "--reason", "demo")
    cli(db, "run", "--seed", "7", "--steps", "60", "--id", "demo")
    out = capsys.readouterr().out
    assert "SIMULATED: not evidence of real profitability" in out and "run demo" in out
    c = open_(db)
    summary = json.loads(c.execute("SELECT summary FROM mm_runs WHERE id = 'demo'").fetchone()[0])
    assert c.execute("SELECT COUNT(*) FROM mm_fills WHERE run_id = 'demo'").fetchone()[0] > 0
    c.close()
    for ccy, p in summary["pnl"].items():
        assert D(p["total"]) == D(p["total_from_ledger"])
        assert f"{D(p['total']):,.2f}" in out
    # The day opened at the run's first quote round; status reads it, and the loss limit beside it.
    cli(db, "status", at="2026-10-30T20:00:00+00:00")
    out = capsys.readouterr().out
    assert "day 2026-10-30 (UTC)" in out and "GBP " in out and "loss limit 20,000" in out
    assert "demo  finished" in out and "ledger integrity: ok" in out


def test_status_writes_nothing(db, capsys):
    cli(db, "status")  # first use seeds the book and limits, as the app does
    c = open_(db)
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    c.close()
    cli(db, "status")
    c = open_(db)
    assert {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables} == before
    c.close()


def test_a_stale_curve_refuses_the_run(db, capsys):
    cli(db, "resume", "--reason", "go")
    with pytest.raises(SystemExit, match="no fresh official curve"):
        cli(db, "run", "--steps", "20", at="2026-11-10T12:00:00+00:00")
    assert "curve is stale" in capsys.readouterr().out


def test_unwind_after_a_kill_only_on_command(db, capsys):
    # A run left with inventory above target (its own unwind turned off), then the kill switch:
    # the inventory stays until someone unwinds it on command, with a reason.
    cli(db, "resume", "--reason", "go")
    c = open_(db)
    params = simulation.load_sim_params(CONFIG, "normal", steps=60, unwind_at_end=False)
    simulation.run(c, "k", "3", INSTRUMENTS, CURVES, NOW.date(), NOW, 96, CONFIG, params)
    mm_id = execution.market_maker_id(c)
    c.close()
    tap = "CENTURY-GBP-2126"
    c = open_(db)  # make sure there is something above the 1m target to unwind
    ledger.trade(c, "extra", next(i for i in INSTRUMENTS if i.id == tap), mm_id, "market",
                 D(100_000), D(90), NOW.date())
    c.close()
    cli(db, "kill", "--reason", "stop")
    with pytest.raises(SystemExit):
        cli(db, "unwind", "k")  # no reason
    cli(db, "unwind", "k", "--reason", "flatten after the kill")
    out = capsys.readouterr().out
    assert "unwind: on command by Ethan: flatten after the kill" in out
    c = open_(db)
    assert c.execute("SELECT COUNT(*) FROM mm_fills WHERE run_id = 'k' AND client = 'unwind'").fetchone()[0] > 0
    c.close()
    with pytest.raises(SystemExit, match="already been unwound"):
        cli(db, "unwind", "k", "--reason", "again")
