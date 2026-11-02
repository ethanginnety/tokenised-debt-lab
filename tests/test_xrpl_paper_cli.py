"""The paper bot's CLI and status report. PAPER."""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest
from paperworld import World

from debtlab.xrpl_market.paper import account, report
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.paper.market import read_market

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import xrpl_paper  # noqa: E402


@pytest.fixture
def w(tmp_path):
    w = World(tmp_path)
    b = w.running_bot()
    w.close(trades=[("book", "sell_xrp", "1.2400", "2")])
    b.tick(w.at())
    w.close()
    b.tick(w.at())
    return w


def cli(w, *args):
    return xrpl_paper.main(["--db", str(w.paper_path), "--monitor-db", str(w.mon_path), *args])


def test_status_shows_both_accounts_side_by_side(w):
    s = report.status(w.paper, w.mon, read_market(w.mon, w.at(), w.cfg), w.at(), w.cfg)
    assert s["label"] == "PAPER" and [a["name"] for a in s["accounts"]] == ["conservative", "optimistic"]
    c = s["accounts"][0]
    assert c["pnl_rlusd"]["fills"] == 1 and c["orders"]["open"] == 2
    p = account.pnl(w.paper, "conservative", D("1.25"))
    assert D(c["pnl_rlusd"]["total"]) == p.total
    text = report.render(s)
    assert text.startswith("=== PAPER ===") and "--- optimistic rule" in text and "markouts RLUSD" in text


def test_the_cli_controls(w, capsys):
    assert cli(w, "status") == 0 and "=== PAPER ===" in capsys.readouterr().out
    assert cli(w, "kill", "--reason", "test") == 0
    assert "4 open paper order(s) cancelled" in capsys.readouterr().out
    assert pdb.state(w.paper)["state"] == "killed"
    assert cli(w, "resume", "--reason", "try") == 2 and "kill switch is on" in capsys.readouterr().out
    assert cli(w, "reset", "--reason", "checked") == 0 and pdb.state(w.paper)["state"] == "paused"
    assert cli(w, "limit", "max_order_eur", "8", "--reason", "smaller") == 0
    assert pdb.limits(w.paper)["max_order_eur"] == D(8)
    row = w.paper.execute("SELECT * FROM paper_limit_changes ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["changed_by"], row["reason"]) == ("Ethan", "smaller")


def test_pause_works_without_a_monitor_database(tmp_path, capsys):
    w = World(tmp_path)
    w.running_bot()
    rc = xrpl_paper.main(["--db", str(w.paper_path), "--monitor-db", str(tmp_path / "none.sqlite3"),
                          "pause", "--reason", "monitor gone"])
    assert rc == 0 and w.orders() == [] and pdb.state(w.paper)["state"] == "paused"
