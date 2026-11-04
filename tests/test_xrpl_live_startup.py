"""The real start-up path: scripts/xrpl_testnet.py with its real wiring. Fake ledger: nothing touches a network.

The real read client runs over fake HTTP, the real signer over a fake websocket, with a fake
key. Only the key file's permission check and the transports are replaced.
"""

import contextlib
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from functools import partial
from pathlib import Path

import pytest
from liveworld import CFG as BASE, ISSUER, RLUSD, SEED, FakeNet, Monitor, rpc_transport, write_key

from debtlab.xrpl_market.live import book, controls
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live import ledger as ledger_module
from debtlab.xrpl_market.live.signer import Submitter, open_cancel_signer, open_signer
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.readonly import ReadOnlyClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import xrpl_testnet  # noqa: E402

CFG = copy.deepcopy(BASE)
CFG["paper"]["strategy"]["vol_min_samples"] = 3
CFG["paper"]["tick_seconds"] = 0.01


def utcnow():
    return datetime.now(timezone.utc)


class Rig:
    def __init__(self, tmp_path, monkeypatch, mainnet="absent", resume_at=None, down_hosts=None):
        self.tmp, self.net = tmp_path, FakeNet()
        now = utcnow()
        self.mon = Monitor(tmp_path, t0=now - timedelta(hours=1), fx_date=now.date())
        for i in range(6, 0, -1):         # recent history for the paper strategy
            self.net.close_ledger()
            self.mon.snapshot(self.net.index, now - timedelta(seconds=i))
        self.key = write_key(tmp_path)
        self.paths = ["--db", str(tmp_path / "live.sqlite3"), "--paper-db", str(tmp_path / "paper.sqlite3"),
                      "--monitor-db", str(self.mon.path)]
        self.reads, self.resume_at = 0, resume_at
        ok = {"acl": lambda p: [], "me": lambda: "me"}
        monkeypatch.setattr(xrpl_testnet, "load_testnet_config", lambda: CFG)
        monkeypatch.setattr(xrpl_testnet, "open_signer", partial(open_signer, **ok))
        monkeypatch.setattr(xrpl_testnet, "open_cancel_signer", partial(open_cancel_signer, **ok))
        monkeypatch.setattr(xrpl_testnet, "Submitter", partial(Submitter, connect=self.net))
        monkeypatch.setattr(xrpl_testnet, "KeepAwake", contextlib.nullcontext)
        monkeypatch.setattr(controls, "ROUND_PAUSE_SECONDS", 0.01)
        transport = rpc_transport(self.net, mainnet=mainnet, on_read=self.on_read, down_hosts=down_hosts)
        monkeypatch.setattr(ledger_module, "ReadOnlyClient", partial(ReadOnlyClient, transport=transport))

    def on_read(self):
        """Time passes: a ledger closes every third read, and the monitor records it."""
        self.reads += 1
        if self.reads % 3 == 0:
            self.net.close_ledger()
            self.mon.snapshot(self.net.index, utcnow())
        if self.resume_at and self.reads == self.resume_at:
            assert self.cli("resume", "--reason", "from the test") == 0

    def cli(self, *args):
        return xrpl_testnet.main([*self.paths, *args])

    def live(self):
        return ldb.open_db(self.tmp / "live.sqlite3")

    def paper(self):
        return pdb.open_db(self.tmp / "paper.sqlite3")


def unknown_offer(net):
    net.offers[55] = {"gets": "1000000", "pays": {"currency": RLUSD, "issuer": ISSUER, "value": "1"}, "expiration": None}


def test_run_quotes_only_after_resume_and_leaves_nothing_behind(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, resume_at=30)
    assert r.cli("run", "--key-file", str(r.key), "--for", "4") == 0
    blobs = r.net.blobs()
    assert blobs and {t["TransactionType"] for t in blobs} >= {"OfferCreate"}
    assert all(t["Flags"] & 0x00010000 for t in blobs if t["TransactionType"] == "OfferCreate")
    assert r.net.offers == {} and set(r.net.urls) == {"wss://testnet.xrpl-labs.com/"}
    out = capsys.readouterr().out
    assert "Quoting only starts after `resume`" in out and "no offers on the ledger" in out and SEED not in out
    conn = r.live()
    assert book.account(conn) is not None and ldb.state(conn)["state"] == "running"
    assert conn.execute("SELECT stop_reason FROM live_runs").fetchone()[0] == "finished (--for)"
    assert r.net.mainnet_reads       # the start-up check did go to mainnet, as a read


def test_the_books_match_the_ledger_through_the_real_read_client(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, resume_at=30)
    r.cli("run", "--key-file", str(r.key), "--for", "4")
    conn = r.live()
    xrp, rlusd = book.balances(conn)
    assert (xrp, rlusd) == (D(r.net.xrp) / 1_000_000, r.net.rlusd)
    assert "disagree" not in ldb.state(conn)["reason"]
    assert not [e for e in conn.execute("SELECT * FROM live_events WHERE kind = 'run_crashed'")]


def test_run_without_resume_sends_nothing(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    assert r.cli("run", "--key-file", str(r.key), "--for", "1") == 0
    assert r.net.blobs() == []


def test_run_refuses_when_mainnet_cant_be_checked(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, mainnet="down")
    assert r.cli("run", "--key-file", str(r.key), "--for", "1") == 2
    assert "refused" in capsys.readouterr().out and r.net.blobs() == [] and r.net.sent == []


def test_status_and_compare_after_a_run(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, resume_at=30)
    r.cli("run", "--key-file", str(r.key), "--for", "4")
    capsys.readouterr()
    assert r.cli("status") == 0 and r.cli("compare") == 0
    out = capsys.readouterr().out
    assert "budget" in out and "TESTNET: no real funds" in out and SEED not in out


def test_a_big_account_runs_on_its_budget_through_the_real_path(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, resume_at=30)
    r.net.xrp, r.net.rlusd = 1000 * 1_000_000, D(100)
    assert r.cli("run", "--key-file", str(r.key), "--for", "4") == 0
    x, a = book.balances(r.live()), book.set_aside(r.live())
    assert (x[0] + a[0], x[1] + a[1]) == (D(r.net.xrp) / 1_000_000, r.net.rlusd)
    assert a[0] > 500 and "set aside" in capsys.readouterr().out and r.net.blobs()


# --- A. kill never depends on mainnet --------------------------------------------------------

def test_kill_cancels_with_every_mainnet_endpoint_unreachable(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, mainnet="down")
    unknown_offer(r.net)
    assert r.cli("kill", "--key-file", str(r.key), "--reason", "test") == 0
    assert "no offers on the ledger" in capsys.readouterr().out
    assert r.net.offers == {} and ldb.state(r.live())["state"] == "killed"
    assert r.net.mainnet_reads == []       # it didn't even try
    assert [t["status"] for t in r.live().execute("SELECT status FROM live_txs")] == ["applied"]   # read back, not lapsed


def test_kill_after_a_real_run_with_mainnet_gone_by_then(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, resume_at=30)
    r.cli("run", "--key-file", str(r.key), "--for", "3")
    unknown_offer(r.net)
    r.net.mainnet_reads.clear()
    assert r.cli("kill", "--key-file", str(r.key), "--reason", "test") == 0
    assert r.net.offers == {} and r.net.mainnet_reads == []


def test_kills_signer_can_only_cancel(tmp_path):
    s = open_cancel_signer(write_key(tmp_path), BASE, acl=lambda p: [], me=lambda: "me")
    assert json.dumps(s.cancel_offer(offer_sequence=5, sequence=6, last_ledger=10, fee_drops=10).kind) == '"OfferCancel"'
    with pytest.raises(TypeError, match="can only cancel"):
        s.create_offer(side="buy", price="0.08", size="1", sequence=6, expiration=800000010, last_ledger=10, fee_drops=10)


def test_kills_signer_keeps_every_other_check(tmp_path):
    from debtlab.xrpl_market import load_config
    from debtlab.xrpl_market.live.guards import KeyFileRefused, MainnetBlocked
    key = write_key(tmp_path)
    with pytest.raises(MainnetBlocked):
        open_cancel_signer(key, load_config(), acl=lambda p: [], me=lambda: "me")
    with pytest.raises(KeyFileRefused, match="can be read by"):
        open_cancel_signer(key, BASE, acl=lambda p: ["Everyone"], me=lambda: "me")
    with pytest.raises(KeyFileRefused, match="inside the repository"):
        open_cancel_signer(ROOT / "x.key", BASE, acl=lambda p: [], me=lambda: "me")


def test_the_submitter_still_refuses_another_network_for_a_cancel(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch)
    unknown_offer(r.net)
    r.net.network_id = 0
    assert r.cli("kill", "--key-file", str(r.key), "--reason", "test") == 1
    assert "couldn't cancel" in capsys.readouterr().out and r.net.blobs() == []
    assert ldb.state(r.live())["state"] == "killed"


def test_only_the_kill_command_uses_the_cancel_only_signer():
    text = (ROOT / "scripts" / "xrpl_testnet.py").read_text(encoding="utf-8")
    run = text[text.index("def _run("):]
    assert "cancel_parts" not in run and "parts(args.key_file, cfg)" in run
    kill = text[text.index("def _kill("):text.index("def _run(")]
    assert "cancel_parts(args.key_file, cfg)" in kill and " parts(args.key_file" not in kill


# --- a second endpoint -----------------------------------------------------------------------

PRIMARY, CLIO = "testnet.xrpl-labs.com", "clio.altnet.rippletest.net"
WSS_PRIMARY, WSS_CLIO = f"wss://{PRIMARY}/", f"wss://{CLIO}/"


def test_run_works_on_the_second_endpoint_when_the_first_is_down(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, resume_at=30, down_hosts={PRIMARY})
    r.net.down_urls.add(WSS_PRIMARY)
    assert r.cli("run", "--key-file", str(r.key), "--for", "4") == 0
    assert r.net.blobs() and r.net.offers == {} and set(r.net.urls) == {WSS_CLIO} and set(r.net.read_hosts) == {CLIO}
    assert "disagree" not in ldb.state(r.live())["reason"]


def test_the_first_endpoint_dying_mid_run_doesnt_stop_it_or_upset_the_books(tmp_path, monkeypatch):
    down = set()
    r = Rig(tmp_path, monkeypatch, resume_at=30, down_hosts=down)
    real = r.on_read

    def on_read():
        real()
        if r.reads == 90:
            down.add(PRIMARY)
            r.net.down_urls.add(WSS_PRIMARY)
    transport = rpc_transport(r.net, on_read=on_read, down_hosts=down)
    monkeypatch.setattr(ledger_module, "ReadOnlyClient", partial(ReadOnlyClient, transport=transport))
    assert r.cli("run", "--key-file", str(r.key), "--for", "4") == 0
    assert {PRIMARY, CLIO} == set(r.net.read_hosts) and r.net.offers == {}
    conn = r.live()
    assert ldb.state(conn)["state"] == "running" and (D(r.net.xrp) / 1_000_000, r.net.rlusd) == book.balances(conn)


def test_kill_works_on_the_second_endpoint(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, mainnet="down", down_hosts={PRIMARY})
    r.net.down_urls.add(WSS_PRIMARY)
    unknown_offer(r.net)
    assert r.cli("kill", "--key-file", str(r.key), "--reason", "test") == 0
    assert r.net.offers == {} and set(r.net.urls) == {WSS_CLIO}


def test_with_both_endpoints_down_kill_still_turns_on_and_says_so(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, down_hosts={PRIMARY, CLIO})
    r.net.down_urls |= {WSS_PRIMARY, WSS_CLIO}
    unknown_offer(r.net)
    monkeypatch.setattr(controls, "kill", partial(controls.kill, timeout=0.05))
    assert r.cli("kill", "--key-file", str(r.key), "--reason", "test") == 1
    assert ldb.state(r.live())["state"] == "killed" and "kill switch ON" in capsys.readouterr().out


def _status_lines(tmp_path, monkeypatch, at):
    monkeypatch.setattr(xrpl_testnet, "now", lambda: at)
    conn = ldb.open_db(tmp_path / "status.sqlite3")
    ldb.seed_limits(conn, CFG, at)
    return conn, lambda: xrpl_testnet.status(conn, tmp_path / "no-monitor.sqlite3", CFG).splitlines()


def test_status_before_the_bot_has_ever_run(tmp_path, monkeypatch):
    _, lines = _status_lines(tmp_path, monkeypatch, utcnow())
    assert lines()[1] == "NOT RUNNING: it has never run"


def test_status_says_when_the_heartbeat_has_gone_quiet(tmp_path, monkeypatch):
    t = utcnow().replace(microsecond=0)      # stored to the millisecond, so a whole second stays exact
    conn, lines = _status_lines(tmp_path, monkeypatch, t)
    ldb.start_run(conn, t - timedelta(seconds=30), 30)
    first = lines()[1]
    assert first.startswith("last alive: ") and first.endswith("(30 s ago)")         # exactly at the limit: still alive
    monkeypatch.setattr(xrpl_testnet, "now", lambda: t + timedelta(seconds=1))
    first = lines()[1]
    assert first.startswith("NOT RUNNING: last alive ") and "31 s ago" in first


def test_status_says_not_running_straight_after_a_clean_stop(tmp_path, monkeypatch):
    t = utcnow()
    conn, lines = _status_lines(tmp_path, monkeypatch, t)
    run, _ = ldb.start_run(conn, t - timedelta(seconds=2), 30)
    ldb.stop_run(conn, run, t - timedelta(seconds=1), "stopped by user")
    assert lines()[1].startswith("NOT RUNNING: stopped at ") and "stopped by user" in lines()[1]


def test_status_shows_not_running_above_a_state_of_running(tmp_path, monkeypatch):
    """A crash or lost power leaves the state at `running`, which alone would read as quoting."""
    t = utcnow()
    conn, lines = _status_lines(tmp_path, monkeypatch, t)
    ldb.start_run(conn, t - timedelta(hours=1), 30)
    controls.resume(conn, "Ethan", "test", t - timedelta(hours=1))
    out = lines()
    assert out[1].startswith("NOT RUNNING") and out[2].startswith("state: running")
