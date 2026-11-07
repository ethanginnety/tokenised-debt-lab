"""M8 mainnet (approved by Ethan, 7 Nov 2026): what keeps the mainnet bot to its account, its key and its limits.

REAL FUNDS on the real network, so every rule here fails without its guard (the controls at the
end). The keys are fakes derived from fixed entropy inside the test, never funded on any network.
Nothing reaches a network: the read client runs over fake HTTP and the signer over a fake
websocket, both answering as mainnet.

    the mainnet guards: only the mainnet allowlist, network 0, the verified issuer, and they
        share nothing with the testnet guards, which still refuse mainnet
    the key: only `xrpl-bot.key`, outside the repository and OneDrive, readable only by Ethan;
        never the master key; only the account's regular key on the validated ledger
    the key script writes the key straight to its file and prints only its address
    start-up: one trust line, to the verified issuer, within its limit and not frozen; no other
        ledger object; no offer the bot didn't place; the account no bigger than its ceiling
    the hard stop: no run or resume after 11 Nov 2026 20:00 UTC, and a running bot stops at it
    the shadow run sends nothing, even after a resume
    a whole run: every submit to the mainnet allowlist only, signed by the regular key, offers
        and cancels only, zero offers left, labelled MAINNET, and the key never shows
"""

import ast
import contextlib
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from functools import partial
from pathlib import Path

import pytest
from liveworld import FakeNet, Monitor, rpc_transport
from xrpl.core import keypairs
from xrpl.core.binarycodec import decode
from xrpl.wallet import Wallet

from debtlab.xrpl_market import load_config, load_mainnet_config, load_testnet_config
from debtlab.xrpl_market.live import controls, guards
from debtlab.xrpl_market.live import db as ldb
from debtlab.xrpl_market.live import ledger as ledger_module
from debtlab.xrpl_market.live.guards import KeyFileRefused, MainnetBlocked
from debtlab.xrpl_market.paper import db as pdb
from debtlab.xrpl_market.readonly import ReadOnlyClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import xrpl_mainnet  # noqa: E402
import xrpl_testnet  # noqa: E402

MASTER_SEED = keypairs.generate_seed(entropy="22" * 16)     # the account's master key: Ethan's, in Xaman only
BOT_SEED = keypairs.generate_seed(entropy="11" * 16)        # the bot's regular key
OTHER_SEED = keypairs.generate_seed(entropy="33" * 16)
ACCOUNT = Wallet.from_seed(MASTER_SEED).address
BOT_KEY = Wallet.from_seed(BOT_SEED).address
OTHER_KEY = Wallet.from_seed(OTHER_SEED).address
MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
LOOKALIKE = "rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK"
TESTNET = load_testnet_config()
MAIN = load_config()
MAINNET_HOSTS = {"xrplcluster.com", "s2.ripple.com"}
SIGNER_FILES = [ROOT / "debtlab" / "xrpl_market" / "live" / "mainnet_signer.py"]
ok = {"acl": lambda p: [], "me": lambda: "me"}


def cfg_for(account=ACCOUNT, **live):
    c = copy.deepcopy(load_mainnet_config())
    c["live"]["account"] = account
    c["live"]["stop_at"] = "2099-01-01T00:00:00Z"
    c["live"].update(live)
    c["paper"]["strategy"]["vol_min_samples"] = 3
    c["paper"]["tick_seconds"] = 0.01
    return c


CFG = cfg_for()


def key(tmp_path, seed=BOT_SEED, name="xrpl-bot.key"):
    p = tmp_path / name
    p.write_text(seed + "\n", encoding="utf-8")
    return p


def mainnet_signer(key_file, cfg=CFG, regular_key=lambda a: BOT_KEY, **kw):
    from debtlab.xrpl_market.live.mainnet_signer import open_mainnet_signer
    return open_mainnet_signer(key_file, cfg, account=cfg["live"]["account"], regular_key=regular_key, **{**ok, **kw})


def offer(s, **kw):
    args = {"side": "buy", "price": "1.2", "size": "7", "sequence": 5, "expiration": 800_000_000,
            "last_ledger": 1000, "fee_drops": 12}
    return s.create_offer(**{**args, **kw})


class Spy:
    """A fake websocket: records every message and answers like a server on `network_id`."""

    def __init__(self, network_id=0):
        self.network_id, self.urls, self.sent, self._out = network_id, [], [], []

    def __call__(self, url, **_):
        self.urls.append(url)
        return self

    def send(self, raw):
        m = json.loads(raw)
        self.sent.append(m)
        result = ({"info": {"network_id": self.network_id}} if m["command"] == "server_info"
                  else {"engine_result": "tesSUCCESS"})
        self._out.append(json.dumps({"id": m["id"], "status": "success", "result": result}))

    def recv(self, timeout=None):
        return self._out.pop(0)

    def close(self):
        pass

    def blobs(self):
        return [m for m in self.sent if m["command"] == "submit"]


# --- the mainnet guards ---------------------------------------------------------------------

@pytest.mark.parametrize("url", [*CFG["live"]["endpoints"]["websocket"], *CFG["live"]["endpoints"]["json_rpc"],
                                 *MAIN["endpoints"]["websocket"], *MAIN["endpoints"]["json_rpc"]])
def test_the_mainnet_endpoints_pass(url):
    assert guards.check_mainnet_endpoint(url) == url


@pytest.mark.parametrize("url", [
    *TESTNET["endpoints"]["websocket"], *TESTNET["live"]["endpoints"]["json_rpc"], "wss://xrpl.ws/",
    "wss://s1.ripple.com/", "https://s1.ripple.com:51234/",
    "ws://xrplcluster.com/", "wss://xrplcluster.com.evil.example/", "wss://evil.example/xrplcluster.com",
    "wss://xrplcluster.com@evil.example/", "wss://user:pw@xrplcluster.com/", "wss://xrplcluster.com:8443/",
    "wss://s.devnet.rippletest.net:51233/", "", "not a url",
])
def test_anything_else_is_refused_as_a_mainnet_endpoint(url):
    with pytest.raises(MainnetBlocked):
        guards.check_mainnet_endpoint(url)


def test_the_two_allowlists_share_nothing_and_testnet_still_refuses_mainnet():
    assert guards.MAINNET_HOSTS == MAINNET_HOSTS and not guards.MAINNET_HOSTS & guards.TESTNET_HOSTS
    for url in CFG["live"]["endpoints"]["websocket"]:
        with pytest.raises(MainnetBlocked):
            guards.check_endpoint(url)
    with pytest.raises(MainnetBlocked):
        guards.check_config(CFG)


@pytest.mark.parametrize("info", [{"info": {"network_id": 1}}, {"info": {"network_id": 2}}, {"info": {}}, {}])
def test_a_server_not_saying_mainnet_is_refused(info):
    with pytest.raises(MainnetBlocked):
        guards.check_mainnet_network(info)
    guards.check_mainnet_network({"info": {"network_id": 0}})


def test_the_mainnet_config_passes_and_says_what_it_is():
    guards.check_mainnet_config(CFG)
    c = load_mainnet_config()
    assert c["network"] == "mainnet" and c["network_id"] == 0 and c["rlusd"]["issuer"] == MAINNET_ISSUER
    assert c["paper"] == MAIN["paper"] and c["paper"]["strategy"]["order_eur"] == "9"      # the frozen strategy
    assert c["live"]["trust_line_limit_rlusd"] == "751088147" and c["live"]["stop_at"] == "2026-11-11T20:00:00Z"
    assert c["offer_expiry"]["max_live_seconds"] == 60 and LOOKALIKE in c["known_lookalikes"]


@pytest.mark.parametrize("change", [
    {"network": "testnet"}, {"network_id": 1}, {"endpoints": TESTNET["endpoints"]},
    {"rlusd": TESTNET["rlusd"]}, {"rlusd": {**MAIN["rlusd"], "issuer": LOOKALIKE}},
    {"live": {**CFG["live"], "endpoints": TESTNET["live"]["endpoints"]}},
])
def test_a_mainnet_config_pointing_anywhere_else_is_refused(change):
    with pytest.raises(MainnetBlocked):
        guards.check_mainnet_config({**CFG, **change})


def test_the_testnet_config_is_refused_as_mainnet():
    with pytest.raises(MainnetBlocked):
        guards.check_mainnet_config(TESTNET)


# --- the key file ---------------------------------------------------------------------------

def test_only_the_bot_key_name_is_accepted_on_mainnet(tmp_path):
    for name in ("xrpl-testnet.key", "xrpl-testnet-taker.key", "bot.key"):
        with pytest.raises(KeyFileRefused, match="xrpl-bot.key"):
            guards.check_mainnet_key_path(key(tmp_path, name=name), ROOT, **ok)
    assert guards.check_mainnet_key_path(key(tmp_path), ROOT, **ok).name == "xrpl-bot.key"


def test_the_bot_key_is_refused_in_the_repository_onedrive_or_readable_by_others(tmp_path, monkeypatch):
    with pytest.raises(KeyFileRefused, match="inside the repository"):
        guards.check_mainnet_key_path(ROOT / "xrpl-bot.key", ROOT, **ok)
    d = tmp_path / "OneDrive"
    d.mkdir()
    with pytest.raises(KeyFileRefused, match="OneDrive"):
        guards.check_mainnet_key_path(key(d), ROOT, **ok)
    with pytest.raises(KeyFileRefused, match="can be read by"):
        guards.check_mainnet_key_path(key(tmp_path), ROOT, acl=lambda p: ["me", "BUILTIN\\Users"], me=lambda: "me")


def test_the_testnet_signer_still_refuses_the_bot_key(tmp_path):
    from debtlab.xrpl_market.live.signer import open_signer
    with pytest.raises(KeyFileRefused, match="mainnet"):
        open_signer(key(tmp_path), TESTNET, on_mainnet=lambda a: False, **ok)


# --- the regular key ------------------------------------------------------------------------

def test_the_master_key_is_refused(tmp_path):
    with pytest.raises(MainnetBlocked, match="master key"):
        mainnet_signer(key(tmp_path, MASTER_SEED), regular_key=lambda a: Wallet.from_seed(MASTER_SEED).address)


@pytest.mark.parametrize("on_ledger", [None, OTHER_KEY, ACCOUNT])
def test_a_key_that_isnt_the_accounts_regular_key_is_refused(tmp_path, on_ledger):
    with pytest.raises(MainnetBlocked, match="regular key"):
        mainnet_signer(key(tmp_path), regular_key=lambda a: on_ledger)


def test_a_testnet_key_renamed_is_still_not_the_regular_key(tmp_path):
    testnet_seed = keypairs.generate_seed(entropy="00" * 16)
    with pytest.raises(MainnetBlocked, match="regular key"):
        mainnet_signer(key(tmp_path, testnet_seed))


def test_the_regular_key_check_is_not_optional(tmp_path):
    from debtlab.xrpl_market.live.mainnet_signer import open_mainnet_signer
    with pytest.raises(TypeError):
        open_mainnet_signer(key(tmp_path), CFG, account=ACCOUNT, **ok)
    with pytest.raises(TypeError):
        open_mainnet_signer(key(tmp_path), CFG, regular_key=lambda a: BOT_KEY, **ok)


def test_when_the_regular_key_cant_be_read_it_is_a_refusal(tmp_path):
    def down(_):
        raise ConnectionError("no answer")
    with pytest.raises(ConnectionError):
        mainnet_signer(key(tmp_path), regular_key=down)


def test_the_config_is_refused_before_the_key_file_is_looked_at(tmp_path):
    from debtlab.xrpl_market.live.mainnet_signer import open_mainnet_signer
    with pytest.raises(MainnetBlocked):
        open_mainnet_signer(tmp_path / "xrpl-bot.key", {**CFG, "endpoints": TESTNET["endpoints"]}, account=ACCOUNT,
                            regular_key=lambda a: BOT_KEY, **ok)


def test_an_unset_account_is_refused(tmp_path):
    with pytest.raises(MainnetBlocked, match="account"):
        mainnet_signer(key(tmp_path), cfg_for(account=""))


def test_the_regular_key_signs_for_the_account_on_the_verified_pair(tmp_path):
    s = mainnet_signer(key(tmp_path))
    assert s.address == ACCOUNT and s.key_address == BOT_KEY and s.network == "mainnet"
    for side in ("buy", "sell"):
        tx = decode(offer(s, side=side).blob)
        assert tx["Account"] == ACCOUNT and tx["TransactionType"] == "OfferCreate"
        assert tx["SigningPubKey"] == Wallet.from_seed(BOT_SEED).public_key
        assert tx["Flags"] & 0x00010000 and tx["Expiration"] == 800_000_000 and tx["LastLedgerSequence"] == 1000
        token = tx["TakerGets"] if side == "buy" else tx["TakerPays"]
        assert token["issuer"] == MAINNET_ISSUER and token["currency"] == CFG["rlusd"]["currency_hex"]
    assert "MAINNET" in repr(s).upper()


def test_the_cancel_signer_only_cancels_and_never_with_the_master_key(tmp_path):
    from debtlab.xrpl_market.live.mainnet_signer import open_mainnet_cancel_signer
    s = open_mainnet_cancel_signer(key(tmp_path), CFG, account=ACCOUNT, **ok)
    tx = decode(s.cancel_offer(offer_sequence=4, sequence=6, last_ledger=1000, fee_drops=12).blob)
    assert tx["TransactionType"] == "OfferCancel" and tx["Account"] == ACCOUNT
    with pytest.raises(TypeError):
        offer(s)
    with pytest.raises(MainnetBlocked, match="master key"):
        open_mainnet_cancel_signer(key(tmp_path, MASTER_SEED), CFG, account=ACCOUNT, **ok)


def test_the_mainnet_signer_module_holds_only_offers_and_cant_print_log_or_read_the_environment():
    for f in SIGNER_FILES:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 and (n.module or "").startswith("xrpl.models.transactions") for a in n.names}
        assert names <= {"OfferCreate", "OfferCreateFlag", "OfferCancel"}
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not imported & {"logging", "os", "pickle", "traceback", "subprocess"}
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"print", "eval", "exec", "vars"}


def test_the_key_never_shows(tmp_path, capsys):
    s = mainnet_signer(key(tmp_path))
    signed = offer(s)
    private = Wallet.from_seed(BOT_SEED).private_key
    shown = [repr(s), str(s), repr(signed), str(signed), signed.blob, *capsys.readouterr()]
    assert not any(BOT_SEED in x or private in x for x in shown)


def test_a_bad_key_file_doesnt_echo_its_contents(tmp_path):
    p = tmp_path / "xrpl-bot.key"
    p.write_text("not-a-seed-CANARY", encoding="utf-8")
    with pytest.raises(KeyFileRefused) as e:
        mainnet_signer(p)
    assert "CANARY" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


# --- the mainnet submitter --------------------------------------------------------------------

@pytest.mark.parametrize("url", [*TESTNET["live"]["endpoints"]["websocket"], "wss://xrpl.ws/"])
def test_a_mainnet_submitter_never_connects_anywhere_else(url):
    from debtlab.xrpl_market.live.signer import Submitter
    spy = Spy()
    with pytest.raises(MainnetBlocked):
        Submitter(url, connect=spy, net=guards.MAINNET)
    assert spy.urls == []


@pytest.mark.parametrize("network_id", [1, 2, None])
def test_a_mainnet_host_on_another_network_gets_nothing(tmp_path, network_id):
    from debtlab.xrpl_market.live.signer import Submitter
    spy = Spy(network_id=network_id)
    sub = Submitter("wss://xrplcluster.com/", connect=spy, net=guards.MAINNET)
    with pytest.raises(MainnetBlocked):
        sub.submit(offer(mainnet_signer(key(tmp_path))))
    assert spy.blobs() == []


def test_the_network_is_checked_before_every_mainnet_submit(tmp_path):
    from debtlab.xrpl_market.live.signer import Submitter
    spy = Spy()
    sub = Submitter("wss://xrplcluster.com/", connect=spy, net=guards.MAINNET)
    s = mainnet_signer(key(tmp_path))
    sub.submit(offer(s, sequence=5))
    spy.network_id = 1
    with pytest.raises(MainnetBlocked):
        sub.submit(offer(s, sequence=6))
    assert len(spy.blobs()) == 1 and [m["command"] for m in spy.sent] == ["server_info", "submit", "server_info"]


def test_the_testnet_submitter_still_refuses_mainnet():
    from debtlab.xrpl_market.live.signer import Submitter
    with pytest.raises(MainnetBlocked):
        Submitter("wss://xrplcluster.com/", connect=Spy())


def test_the_shadow_submitter_sends_nothing_but_server_info(tmp_path):
    from debtlab.xrpl_market.live.mainnet_signer import ShadowSubmitter
    from debtlab.xrpl_market.live.signer import SubmitFailed
    spy = Spy()
    sub = ShadowSubmitter("wss://xrplcluster.com/", connect=spy)
    assert sub.alive()
    with pytest.raises(SubmitFailed) as e:
        sub.submit(offer(mainnet_signer(key(tmp_path))))
    assert e.value.sent is False and spy.blobs() == [] and {m["command"] for m in spy.sent} == {"server_info"}


# --- the key script ---------------------------------------------------------------------------

def test_keygen_writes_the_key_to_its_file_and_prints_only_its_address(tmp_path, monkeypatch, capsys):
    locked = []
    monkeypatch.setattr(xrpl_mainnet, "lock_folder", locked.append)
    monkeypatch.setattr(xrpl_mainnet, "KEY_CHECKS", ok)
    p = tmp_path / "secrets" / "xrpl-bot.key"
    assert xrpl_mainnet.main(["keygen", "--key-file", str(p)]) == 0
    seed = p.read_text(encoding="utf-8").strip()
    address = Wallet.from_seed(seed).address
    out = capsys.readouterr()
    assert address in out.out and seed not in out.out + out.err
    assert Wallet.from_seed(seed).private_key not in out.out + out.err
    assert locked == [p.parent] and keypairs.generate_seed(entropy="00" * 16) != seed


def test_keygen_never_overwrites_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(xrpl_mainnet, "lock_folder", lambda d: None)
    monkeypatch.setattr(xrpl_mainnet, "KEY_CHECKS", ok)
    p = key(tmp_path)
    assert xrpl_mainnet.main(["keygen", "--key-file", str(p)]) == 2
    assert p.read_text(encoding="utf-8").strip() == BOT_SEED and BOT_SEED not in capsys.readouterr().out


@pytest.mark.parametrize("where", ["repo", "onedrive", "name"])
def test_keygen_refuses_a_bad_place(tmp_path, monkeypatch, where):
    monkeypatch.setattr(xrpl_mainnet, "lock_folder", lambda d: None)
    monkeypatch.setattr(xrpl_mainnet, "KEY_CHECKS", ok)
    p = {"repo": ROOT / "data" / "xrpl-bot.key", "onedrive": tmp_path / "OneDrive" / "xrpl-bot.key",
         "name": tmp_path / "bot.key"}[where]
    assert xrpl_mainnet.main(["keygen", "--key-file", str(p)]) == 2
    assert not p.exists()


def test_two_keys_are_never_the_same(tmp_path):
    from debtlab.xrpl_market.live.mainnet_signer import generate_bot_key
    a = generate_bot_key(tmp_path / "a" / "xrpl-bot.key", repo_root=ROOT)
    b = generate_bot_key(tmp_path / "b" / "xrpl-bot.key", repo_root=ROOT)
    assert a != b and a.startswith("r") and Wallet.from_seed((tmp_path / "a" / "xrpl-bot.key").read_text(encoding="utf-8").strip()).address == a


# --- the whole mainnet bot, over a fake mainnet -------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc)


class MainRig:
    def __init__(self, tmp_path, monkeypatch, resume_at=None, cfg=None, shadow=False, xrp="20", rlusd="25"):
        self.tmp, self.cfg = tmp_path, cfg or CFG
        self.net = FakeNet(xrp=xrp, rlusd=rlusd, address=ACCOUNT, issuer=MAINNET_ISSUER, network_id=0, regular_key=BOT_KEY)
        now = utcnow()
        self.mon = Monitor(tmp_path, network="mainnet", t0=now - timedelta(hours=1), fx_date=now.date(), exclude=ACCOUNT)
        for i in range(6, 0, -1):
            self.net.close_ledger()
            self.mon.snapshot(self.net.index, now - timedelta(seconds=i), bid="1.1995", ask="1.2005", amm_price="1.2")
        self.key = key(tmp_path)
        self.paths = ["--db", str(tmp_path / "live.sqlite3"), "--paper-db", str(tmp_path / "paper.sqlite3"),
                      "--monitor-db", str(self.mon.path)]
        self.reads, self.resume_at = 0, resume_at
        self.net.best_bid, self.net.best_ask = D("1.1995"), D("1.2005")
        from debtlab.xrpl_market.live import mainnet_signer as ms
        from debtlab.xrpl_market.live.signer import Submitter
        monkeypatch.setattr(xrpl_mainnet, "load_mainnet_config", lambda: self.cfg)
        monkeypatch.setattr(xrpl_mainnet, "open_mainnet_signer", partial(ms.open_mainnet_signer, **ok))
        monkeypatch.setattr(xrpl_mainnet, "open_mainnet_cancel_signer", partial(ms.open_mainnet_cancel_signer, **ok))
        monkeypatch.setattr(xrpl_mainnet, "Submitter", partial(Submitter, connect=self.net))
        monkeypatch.setattr(xrpl_mainnet, "ShadowSubmitter", partial(ms.ShadowSubmitter, connect=self.net))
        monkeypatch.setattr(xrpl_mainnet, "KeepAwake", contextlib.nullcontext)
        monkeypatch.setattr(controls, "ROUND_PAUSE_SECONDS", 0.01)
        transport = rpc_transport(self.net, on_read=self.on_read, ours=MAINNET_HOSTS)
        monkeypatch.setattr(ledger_module, "ReadOnlyClient", partial(ReadOnlyClient, transport=transport))

    def on_read(self):
        self.reads += 1
        if self.reads % 3 == 0:
            self.net.close_ledger()
            self.mon.snapshot(self.net.index, utcnow(), bid="1.1995", ask="1.2005", amm_price="1.2")
        if self.resume_at and self.reads == self.resume_at:
            assert self.cli("resume", "--reason", "from the test") == 0

    def cli(self, *args):
        return xrpl_mainnet.main([*self.paths, *args])

    def live(self):
        return ldb.open_db(self.tmp / "live.sqlite3", network="mainnet")

    def submits(self):
        return [m for m in self.net.sent if m["command"] == "submit"]


def test_a_whole_mainnet_run_stays_on_mainnet_with_the_regular_key(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch, resume_at=30)
    assert r.cli("run", "--key-file", str(r.key), "--for", "4") == 0
    blobs = r.net.blobs()
    assert blobs and {t["TransactionType"] for t in blobs} <= {"OfferCreate", "OfferCancel"}
    assert "OfferCreate" in {t["TransactionType"] for t in blobs}
    pub = Wallet.from_seed(BOT_SEED).public_key
    assert all(t["Account"] == ACCOUNT and t["SigningPubKey"] == pub for t in blobs)
    assert all(t["TakerGets"]["issuer"] == MAINNET_ISSUER if isinstance(t.get("TakerGets"), dict) else True for t in blobs)
    assert {u.split("/")[2] for u in r.net.urls} <= MAINNET_HOSTS and r.net.mainnet_reads == []
    assert set(r.net.read_hosts) <= MAINNET_HOSTS
    assert r.net.offers == {}
    out = capsys.readouterr().out
    assert "MAINNET" in out and "TESTNET" not in out
    conn = r.live()
    labels = {json.loads(e["detail"]).get("label") for e in conn.execute("SELECT detail FROM live_events")} - {None}
    assert labels == {guards.MAINNET.label}
    dump = "\n".join(conn.iterdump()) + (tmp_path / "paper.sqlite3").read_bytes().decode("latin-1")
    assert BOT_SEED not in dump + out and Wallet.from_seed(BOT_SEED).private_key not in dump + out


def test_the_shadow_run_reads_everything_and_sends_nothing_even_after_resume(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch, resume_at=30)
    assert r.cli("run", "--shadow", "--key-file", str(r.key), "--for", "3") == 0
    assert r.submits() == [] and r.net.read_hosts and r.net.offers == {}
    assert "SHADOW" in capsys.readouterr().out


def test_check_opens_the_key_and_runs_the_start_up_checks_without_sending(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch)
    assert r.cli("check", "--key-file", str(r.key)) == 0
    out = capsys.readouterr().out
    assert ACCOUNT in out and BOT_KEY in out and "all checks passed" in out
    assert r.net.sent == [] or {m["command"] for m in r.net.sent} <= {"server_info"}


def refused(r, capsys, *needles):
    assert r.cli("run", "--key-file", str(r.key), "--for", "2") == 2
    out = capsys.readouterr().out
    assert r.submits() == [], out
    for n in needles:
        assert n in out, out
    return out


def test_the_wrong_regular_key_on_the_ledger_refuses_to_start(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch)
    r.net.regular_key = OTHER_KEY
    refused(r, capsys, "regular key")


@pytest.mark.parametrize("lines,needle", [
    ([], "no RLUSD trust line"),
    ("two", "exactly one trust line"),
    ([{"currency": "524C555344000000000000000000000000000000", "account": LOOKALIKE, "balance": "25", "limit": "100"}],
     "exactly one trust line"),
    ([{"currency": "524C555344000000000000000000000000000000", "account": MAINNET_ISSUER, "balance": "25", "limit": "751088148"}],
     "limit"),
    ([{"currency": "524C555344000000000000000000000000000000", "account": MAINNET_ISSUER, "balance": "25", "limit": "100",
       "freeze_peer": True}], "frozen"),
])
def test_a_trust_line_out_of_order_refuses_to_start(tmp_path, monkeypatch, capsys, lines, needle):
    r = MainRig(tmp_path, monkeypatch)
    if lines == "two":
        lines = [{"currency": "524C555344000000000000000000000000000000", "account": MAINNET_ISSUER, "balance": "25",
                  "limit": "100"}, {"currency": "USD", "account": OTHER_KEY, "balance": "0", "limit": "5"}]
    r.net.lines = lines
    refused(r, capsys, needle)


@pytest.mark.parametrize("kind", ["SignerList", "Escrow", "Check", "PayChannel", "Ticket"])
def test_another_ledger_object_refuses_to_start(tmp_path, monkeypatch, capsys, kind):
    r = MainRig(tmp_path, monkeypatch)
    r.net.objects = [{"LedgerEntryType": kind}]
    refused(r, capsys, kind)


def test_an_offer_the_bot_didnt_place_refuses_to_start_and_is_left_alone(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch)
    r.net.offers[55] = {"gets": "1000000", "pays": {"currency": CFG["rlusd"]["currency_hex"], "issuer": MAINNET_ISSUER,
                                                    "value": "1.3"}, "expiration": None}
    refused(r, capsys, "55")
    assert 55 in r.net.offers


def test_an_account_bigger_than_its_ceiling_refuses_to_start(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch, xrp="2000")
    refused(r, capsys, "max_account_eur")


def test_an_issuer_global_freeze_refuses_to_start(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch)
    r.net.issuer_flags = 0x00400000
    refused(r, capsys, "freeze")


def test_no_run_or_resume_after_the_hard_stop(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch, cfg=cfg_for(stop_at="2020-01-01T00:00:00Z"))
    refused(r, capsys, "hard stop")
    assert r.cli("resume", "--reason", "late") == 2
    assert "hard stop" in capsys.readouterr().out and ldb.state(r.live())["state"] != "running"


def test_a_running_bot_stops_itself_at_the_hard_stop(tmp_path, monkeypatch, capsys):
    stop = utcnow() + timedelta(seconds=3)
    r = MainRig(tmp_path, monkeypatch, resume_at=20, cfg=cfg_for(stop_at=stop.strftime("%Y-%m-%dT%H:%M:%S.%fZ")))
    assert r.cli("run", "--key-file", str(r.key), "--for", "60") == 0
    assert utcnow() < stop + timedelta(seconds=30) and r.net.offers == {}
    assert "hard stop" in capsys.readouterr().out


def test_kill_works_with_the_bot_down_and_leaves_zero_offers(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch, resume_at=30)
    assert r.cli("run", "--key-file", str(r.key), "--for", "3") == 0
    r.net.offers[77] = {"gets": "1000000", "pays": {"currency": CFG["rlusd"]["currency_hex"], "issuer": MAINNET_ISSUER,
                                                    "value": "1.3"}, "expiration": None}
    assert r.cli("kill", "--key-file", str(r.key), "--reason", "test") == 0
    assert r.net.offers == {} and ldb.state(r.live())["state"] == "killed"


# --- databases are kept apart -------------------------------------------------------------------

def test_a_testnet_live_database_is_refused_on_mainnet_and_the_other_way_round(tmp_path):
    t = ldb.open_db(tmp_path / "t.sqlite3")
    ldb.set_state(t, "paused", "test", "used", utcnow())
    t.commit()
    t.close()
    with pytest.raises(ldb.WrongDatabase, match="testnet"):
        ldb.open_db(tmp_path / "t.sqlite3", network="mainnet")
    m = ldb.open_db(tmp_path / "m.sqlite3", network="mainnet")
    assert ldb.network(m) == "mainnet"
    m.close()
    with pytest.raises(ldb.WrongDatabase, match="mainnet"):
        ldb.open_db(tmp_path / "m.sqlite3")


def test_the_mainnet_bot_refuses_the_testnet_monitor(tmp_path, monkeypatch, capsys):
    r = MainRig(tmp_path, monkeypatch)
    store_conn = r.mon.conn
    store_conn.execute("UPDATE xrpl_meta SET value = 'testnet' WHERE key = 'network'")
    store_conn.commit()
    refused(r, capsys, "mainnet")


def test_paper_alongside_mainnet_is_marked_and_kept_out_of_the_m7_evidence(tmp_path, monkeypatch, capsys):
    from debtlab.xrpl_market.live.alongside import open_alongside_paper
    import xrpl_paper
    c = open_alongside_paper(tmp_path / "p.sqlite3", "mainnet")
    c.close()
    with pytest.raises(pdb.WrongDatabase):
        open_alongside_paper(tmp_path / "p.sqlite3", "testnet")
    assert xrpl_paper.main(["--db", str(tmp_path / "p.sqlite3"), "--monitor-db", str(tmp_path / "none.sqlite3"),
                            "status"]) == 2
    m7 = pdb.open_db(tmp_path / "m7.sqlite3")
    pdb.set_state(m7, "paused", "test", "M7 evidence", utcnow())
    m7.commit()
    m7.close()
    with pytest.raises(pdb.WrongDatabase, match="M7"):
        open_alongside_paper(tmp_path / "m7.sqlite3", "mainnet")


def test_the_testnet_script_is_unchanged_in_what_it_opens():
    assert xrpl_testnet.load_testnet_config is load_testnet_config
    assert "mainnet" not in xrpl_testnet.DEFAULT_DB.name


# --- controls: take a guard away and the tests above catch it -----------------------------------

def off(monkeypatch, name):
    monkeypatch.setattr(guards, name, lambda *a, **k: a[0] if a else None)


def test_control_without_the_mainnet_endpoint_guard_a_testnet_connection_happens(monkeypatch):
    from debtlab.xrpl_market.live.signer import Submitter
    off(monkeypatch, "check_mainnet_endpoint")
    spy = Spy()
    Submitter(TESTNET["live"]["endpoints"]["websocket"][0], connect=spy, net=guards.MAINNET).alive()
    assert spy.urls == [TESTNET["live"]["endpoints"]["websocket"][0]]


def test_control_without_the_mainnet_network_guard_a_blob_reaches_network_1(monkeypatch, tmp_path):
    from debtlab.xrpl_market.live.signer import Submitter
    off(monkeypatch, "check_mainnet_network")
    spy = Spy(network_id=1)
    Submitter("wss://xrplcluster.com/", connect=spy, net=guards.MAINNET).submit(offer(mainnet_signer(key(tmp_path))))
    assert len(spy.blobs()) == 1


def test_control_without_the_regular_key_guard_the_master_key_signs(monkeypatch, tmp_path):
    off(monkeypatch, "check_regular_key")
    s = mainnet_signer(key(tmp_path, MASTER_SEED), regular_key=lambda a: None)
    assert decode(offer(s).blob)["SigningPubKey"] == Wallet.from_seed(MASTER_SEED).public_key


def test_control_without_the_mainnet_config_guard_the_key_file_is_reached(monkeypatch, tmp_path):
    from debtlab.xrpl_market.live.mainnet_signer import open_mainnet_signer
    off(monkeypatch, "check_mainnet_config")
    with pytest.raises(KeyFileRefused):
        open_mainnet_signer(tmp_path / "xrpl-bot.key", {**CFG, "endpoints": TESTNET["endpoints"]}, account=ACCOUNT,
                            regular_key=lambda a: BOT_KEY, **ok)


def test_control_without_the_key_name_guard_a_testnet_key_file_is_read(monkeypatch, tmp_path):
    monkeypatch.setattr(guards, "check_mainnet_key_path", lambda p, *a, **k: Path(p))
    testnet_seed = keypairs.generate_seed(entropy="00" * 16)
    s = mainnet_signer(key(tmp_path, testnet_seed, "xrpl-testnet.key"),
                       regular_key=lambda a: Wallet.from_seed(testnet_seed).address)
    assert s.address == ACCOUNT


def test_control_without_the_start_up_checks_a_signer_list_doesnt_stop_it(tmp_path, monkeypatch, capsys):
    from debtlab.xrpl_market.live import setup
    monkeypatch.setattr(setup, "problems", lambda *a, **k: [])
    r = MainRig(tmp_path, monkeypatch)
    r.net.objects = [{"LedgerEntryType": "SignerList"}]
    assert r.cli("run", "--key-file", str(r.key), "--for", "1") == 0
