"""M8 step 7: paper alongside the testnet bot, the comparison, the command line and the crossing helper. No network."""

import copy
import importlib.util
import json
import re
import sys
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from liveworld import ADDRESS, CFG as BASE, SEED, T0, FakeNet, Monitor, client_for, write_key
from xrpl.core.binarycodec import decode

from debtlab.xrpl_market import load_config, store
from debtlab.xrpl_market.live import book, compare, controls
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live.alongside import Alongside, open_testnet_paper
from debtlab.xrpl_market.live.bot import LiveBot
from debtlab.xrpl_market.live.guards import MainnetBlocked
from debtlab.xrpl_market.live.ledger import TestnetLedger
from debtlab.xrpl_market.live.signer import Submitter, open_signer
from debtlab.xrpl_market.live.venue import LiveVenue
from debtlab.xrpl_market.paper import account
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.trades import Trade

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import xrpl_paper  # noqa: E402
import xrpl_testnet  # noqa: E402

CFG = copy.deepcopy(BASE)
CFG["paper"]["strategy"]["vol_min_samples"] = 3


class World:
    def __init__(self, tmp_path, **net):
        self.tmp, self.net, self.mon = tmp_path, FakeNet(**net), Monitor(tmp_path)
        self.live_path, self.paper_path = tmp_path / "live.sqlite3", tmp_path / "paper.sqlite3"
        self.conn, self.paper = ldb.open_db(self.live_path), open_testnet_paper(self.paper_path)
        self.t, self.lines, self.ntx = T0, [], 0
        self.mon.snapshot(self.net.index, self.t)
        self.parts()
        self.side = Alongside(self.conn, self.paper, self.mon.path, CFG, out=self.lines.append)
        self.bot = LiveBot(self.conn, self.mon.path, CFG, self.ledger, self.venue, self.sub, self.side.wants,
                           alongside=self.side, out=self.lines.append)

    def parts(self):
        self.ledger = TestnetLedger(CFG, client_for(self.net))
        self.signer = open_signer(write_key(self.tmp), CFG, on_mainnet=lambda a: False, acl=lambda p: [], me=lambda: "me")
        self.sub = Submitter(CFG["endpoints"]["websocket"][0], connect=self.net)
        self.venue = LiveVenue(self.signer, self.sub, CFG)

    def advance(self, trades=(), dt=4.0, **snap):
        self.t += timedelta(seconds=dt)
        self.net.close_ledger()
        if trades:
            store.record_trades(self.mon.conn, [
                Trade(h, f"N{i}", self.net.index, "book", side, D(p), D(x), D(p) * D(x), "rMaker", "OfferCreate")
                for i, (h, side, p, x) in enumerate(trades)], self.t - timedelta(seconds=1))
        self.mon.snapshot(self.net.index, self.t, **snap)

    def step(self, n=1, **kw):
        r = None
        for _ in range(n):
            self.advance(**kw)
            r = self.bot.tick(self.t + timedelta(seconds=0.5))
        return r

    def running(self):
        for _ in range(5):        # history for the paper strategy's volatility
            self.advance()
        self.bot.start(self.t)
        controls.resume(self.conn, "Ethan", "test", self.t)
        self.step(6)
        while len(book.open_orders(self.conn)) < 2 or book.open_orders(self.conn)[0]["status"] != "open":
            self.step()
        return self

    def paper_orders(self, acct="conservative"):
        return {o["side"]: o for o in account.open_orders(self.paper, acct)}

    def live_orders(self):
        return {o["side"]: o for o in book.open_orders(self.conn)}


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_live_rests_exactly_the_paper_quotes(w):
    w.running()
    live, cons, opt = w.live_orders(), w.paper_orders(), w.paper_orders("optimistic")
    for side in ("buy", "sell"):
        assert live[side]["price"] == cons[side]["price"] == opt[side]["price"]
        assert live[side]["paper_order_id"] == cons[side]["id"] and live[side]["decision_id"] == cons[side]["decision_id"]


def test_a_paper_requote_moves_the_live_quote(w):
    w.running()
    old = w.live_orders()["buy"]["price"]
    w.step(4, bid="0.0845", ask="0.0855", amm_price="0.085")
    live, cons = w.live_orders(), w.paper_orders()
    assert live["buy"]["price"] != old and live["buy"]["price"] == cons["buy"]["price"]


def test_a_real_fill_is_compared_with_both_rules_and_flagged_as_our_own(w):
    w.running()
    o = w.live_orders()["buy"]
    price = D(o["price"])
    w.net.hit(o["sequence"], "5")
    w.advance()
    h = w.net.history[-1]["hash"]
    store.record_trades(w.mon.conn, [Trade(h, "N", w.net.index, "book", "sell_xrp", price, D(5), price * 5, ADDRESS, "OfferCreate")], w.t)
    w.bot.tick(w.t + timedelta(seconds=0.5))
    w.step(2)
    rows = compare.compare(w.conn, w.paper)
    (r,) = [x for x in rows if x.decision_id == o["decision_id"]]
    assert (r.live_maker, r.conservative, r.optimistic, r.own_trades) == (D(5), D(0), D(5), 1) and r.kind == "both"
    assert "own live fill" in compare.render(rows, compare.fees(w.conn, w.paper))


def test_a_trade_that_doesnt_touch_our_offer_is_paper_only(w):
    w.running()
    bid = D(w.paper_orders()["buy"]["price"])
    w.step(1, trades=[("T" * 64, "sell_xrp", str(bid - D("0.0001")), "3")])
    w.step(2)
    rows = compare.compare(w.conn, w.paper)
    assert [(r.kind, r.conservative, r.optimistic) for r in rows] == [("paper only", D(3), D(3))]


def test_a_live_fill_paper_didnt_see_is_live_only(w):
    w.running()
    w.net.hit(w.live_orders()["sell"]["sequence"], "2")
    w.step(2)
    (r,) = compare.compare(w.conn, w.paper)
    assert r.kind == "live only" and r.live_maker == 2


def test_fees_are_shown_for_both(w):
    w.running()
    f = compare.fees(w.conn, w.paper)
    assert f["live"] > 0 and f["conservative"] > 0 and f["live_transactions"] >= 2
    assert "says nothing about profit" in compare.render([], f)


def test_a_live_pause_pauses_paper(w):
    w.running()
    controls.pause(w.conn, "Ethan", "test", w.t)
    w.step(2)
    assert pdb.state(w.paper)["state"] == "paused" and account.open_orders(w.paper) == []
    assert all(o.get("expiration", 0) <= w.net.close_time for o in w.net.offers.values())


def test_a_breach_on_a_paper_account_pauses_live_too(w):
    w.running()
    pdb.set_limit(w.paper, "max_daily_loss_eur", D("0.01"), "Ethan", "test", w.t)
    w.step(2, bid="0.0695", ask="0.0705", amm_price="0.07")
    assert pdb.state(w.paper)["changed_by"] == "risk check"
    assert ldb.state(w.conn)["state"] == "paused" and "paper" in ldb.state(w.conn)["reason"]


def test_a_live_breach_pauses_paper_too(w):
    w.running()
    ldb.set_limit(w.conn, "max_daily_loss_eur", D("0.01"), "Ethan", "test", w.t)
    w.step(2, bid="0.0695", ask="0.0705", amm_price="0.07")
    assert ldb.state(w.conn)["state"] == "paused" and pdb.state(w.paper)["state"] == "paused"


def test_the_kill_switch_reaches_paper(w):
    w.running()
    controls.kill(w.conn, w.ledger, w.venue, CFG, "Ethan", "t", 10, clock=lambda: w.t, sleep=lambda _: w.advance())
    w.bot.tick(w.t + timedelta(seconds=1))
    assert pdb.state(w.paper)["state"] == "killed" and account.open_orders(w.paper) == [] and w.net.offers == {}


def test_paper_doesnt_quote_until_live_is_resumed(tmp_path):
    w = World(tmp_path)
    for _ in range(5):
        w.advance()
    w.bot.start(w.t)
    w.step(3)
    assert account.open_orders(w.paper) == [] and w.net.blobs() == []


# --- databases -------------------------------------------------------------------------------

def test_a_used_m7_paper_database_is_refused(tmp_path):
    p = tmp_path / "m7.sqlite3"
    conn = pdb.open_db(p)
    pdb.set_state(conn, "running", "Ethan", "M7", T0)
    conn.commit()
    conn.close()
    with pytest.raises(pdb.WrongDatabase, match="M7 paper trading"):
        open_testnet_paper(p)


def test_the_m7_paper_command_refuses_a_testnet_paper_database(tmp_path, capsys):
    p = tmp_path / "t.sqlite3"
    open_testnet_paper(p).close()
    assert xrpl_paper.main(["--db", str(p), "--monitor-db", str(tmp_path / "none"), "status"]) == 2
    assert "testnet" in capsys.readouterr().out


# --- the command line ------------------------------------------------------------------------

@pytest.fixture
def cli(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(xrpl_testnet, "parts", lambda key, cfg: (w.signer, w.sub, w.ledger, w.venue))
    monkeypatch.setattr(xrpl_testnet, "load_testnet_config", lambda: CFG)
    monkeypatch.setattr(xrpl_testnet.time, "sleep", lambda _: w.advance())

    def run(*args):
        return xrpl_testnet.main(["--db", str(w.live_path), "--paper-db", str(w.paper_path), "--monitor-db", str(w.mon.path), *args])
    w.run = run
    return w


def test_cli_starts_paused_and_status_says_so(cli, capsys):
    assert cli.run("status") == 0
    out = capsys.readouterr().out
    assert "TESTNET: no real funds" in out and "state: paused" in out and "not opened" in out


def test_cli_resume_pause_reset(cli, capsys):
    assert cli.run("resume", "--reason", "go") == 0
    assert ldb.state(cli.conn)["state"] == "running" and pdb.state(cli.paper)["state"] == "running"
    assert cli.run("pause", "--reason", "stop") == 0
    assert ldb.state(cli.conn)["state"] == "paused"
    assert cli.run("reset", "--reason", "x") == 2
    assert "not on" in capsys.readouterr().out


def test_cli_kill_with_the_bot_down_leaves_zero_offers(cli, capsys):
    cli.running()
    assert cli.run("kill", "--key-file", "k", "--reason", "test") == 0
    assert "no offers on the ledger" in capsys.readouterr().out
    assert cli.net.offers == {} and ldb.state(cli.conn)["state"] == "killed"
    assert cli.run("resume", "--reason", "x") == 2
    assert ldb.state(cli.conn)["state"] == "killed" and pdb.state(cli.paper)["state"] != "running"


def test_cli_kill_is_on_even_if_the_key_is_refused(tmp_path, capsys, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(xrpl_testnet, "load_testnet_config", lambda: CFG)
    code = xrpl_testnet.main(["--db", str(w.live_path), "--paper-db", str(w.paper_path), "--monitor-db", str(w.mon.path),
                              "kill", "--key-file", str(ROOT / "inside-the-repo.key"), "--reason", "test"])
    assert code == 1 and ldb.state(w.conn)["state"] == "killed"
    assert "kill switch ON" in capsys.readouterr().out


def test_cli_limit_change_is_recorded_with_why(cli, capsys):
    assert cli.run("limit", "max_total_eur", "80", "--reason", "testnet account") == 0
    r = cli.conn.execute("SELECT * FROM live_limit_changes WHERE name = 'max_total_eur' ORDER BY id DESC").fetchone()
    assert r["value"] == "80" and r["reason"] == "testnet account" and r["changed_by"] == "Ethan"


def test_cli_compare_and_status_with_activity(cli, capsys):
    cli.running()
    assert cli.run("status") == 0 and cli.run("compare") == 0
    out = capsys.readouterr().out
    assert ADDRESS in out and "No fills yet" in out and SEED not in out


def test_cli_run_is_given_no_way_to_skip_the_key_file():
    with pytest.raises(SystemExit):
        xrpl_testnet.main(["run"])


def test_the_command_line_has_no_way_to_give_a_seed():
    text = (ROOT / "scripts" / "xrpl_testnet.py").read_text(encoding="utf-8")
    assert not re.search(r"--(seed|secret|key)(?![-\w])", text) and "getenv" not in text and "environ" not in text


# --- the crossing helper ---------------------------------------------------------------------

def load_taker():
    spec = importlib.util.spec_from_file_location("taker_helper", ROOT / "tests" / "live_testnet" / "taker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_helper_is_not_passive_and_rests_nothing():
    t = load_taker()
    from xrpl.core import keypairs
    second = keypairs.generate_seed(entropy="11" * 16)
    s = t.build(second, CFG, "sell", D("0.0799"), D(10), 5, 1004, ADDRESS)
    tx = decode(s.blob)
    assert tx["Flags"] & 0x00020000 and not tx["Flags"] & 0x00010000
    assert tx["TakerPays"]["issuer"] == CFG["rlusd"]["issuer"] and tx["LastLedgerSequence"] == 1004


def test_the_helper_refuses_mainnet_the_bots_key_and_big_sizes():
    t = load_taker()
    from xrpl.core import keypairs
    second = keypairs.generate_seed(entropy="11" * 16)
    with pytest.raises(MainnetBlocked):
        t.build(second, load_config(), "sell", D("1"), D(1), 1, 2, ADDRESS)
    with pytest.raises(t.Refused, match="second testnet key"):
        t.build(SEED, CFG, "sell", D("0.08"), D(1), 1, 2, ADDRESS)
    with pytest.raises(t.Refused, match="between"):
        t.build(second, CFG, "sell", D("0.08"), D(201), 1, 2, ADDRESS)


def test_nothing_the_bot_runs_can_import_the_helper():
    rx = re.compile(r"live_testnet|\btaker_helper\b|import\s+taker\b|from\s+taker\b")
    found = [p for p in [*ROOT.glob("debtlab/**/*.py"), *ROOT.glob("scripts/**/*.py")] if rx.search(p.read_text(encoding="utf-8"))]
    assert found == []


def test_the_only_non_passive_offer_is_in_the_helper():
    found = [p for p in [*ROOT.glob("debtlab/**/*.py"), *ROOT.glob("scripts/**/*.py")]
             if re.search(r"TF_IMMEDIATE_OR_CANCEL|TF_FILL_OR_KILL|tfImmediateOrCancel", p.read_text(encoding="utf-8"))]
    assert found == []


def test_the_helper_has_no_print_of_the_seed():
    text = (ROOT / "tests" / "live_testnet" / "taker.py").read_text(encoding="utf-8")
    assert not re.search(r"print\([^)]*seed", text)


def test_a_full_scenario_never_leaks_the_seed_or_leaves_testnet(w, capsys):
    w.running()
    w.step(8)
    w.net.hit(next(iter(w.live_orders().values()))["sequence"], "1")
    w.step(2)
    controls.kill(w.conn, w.ledger, w.venue, CFG, "Ethan", "t", 10, clock=lambda: w.t, sleep=lambda _: w.advance())
    assert set(w.net.urls) == {"wss://testnet.xrpl-labs.com/"} and w.net.blobs()
    for conn in (w.conn, w.paper):
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for rec in conn.execute(f"SELECT * FROM {row['name']}").fetchall():
                assert SEED not in " ".join(str(v) for v in tuple(rec))
    out = capsys.readouterr()
    assert SEED not in out.out + out.err + json.dumps(w.lines)
