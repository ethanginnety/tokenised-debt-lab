"""M8: mainnet can't be signed for, the key stays secret, and only offers can be built.

The key here is a fake: derived from all-zero entropy inside the test, never funded on any
network and worth nothing. Nothing reaches a network: the connection is a recording fake.
"""

import ast
import json
import logging
import pickle
from pathlib import Path

import pytest
from xrpl.core import keypairs
from xrpl.core.binarycodec import decode
from xrpl.wallet import Wallet

from debtlab.xrpl_market import load_config
from debtlab.xrpl_market.live import guards
from debtlab.xrpl_market.live.guards import KeyFileRefused, MainnetBlocked

ROOT = Path(__file__).resolve().parent.parent
TESTNET = load_config(ROOT / "config" / "xrpl_testnet.json")
MAINNET = load_config(ROOT / "config" / "xrpl.json")
MAINNET_URLS = sorted({*MAINNET["endpoints"]["websocket"], *MAINNET["endpoints"]["json_rpc"],
                       "wss://s1.ripple.com/", "wss://xrpl.ws/", "https://s1.ripple.com:51234/"})
FAKE_SEED = keypairs.generate_seed(entropy="00" * 16)
SIGNER_FILE = ROOT / "debtlab" / "xrpl_market" / "live" / "signer.py"
needs_signer = pytest.mark.xfail(raises=(ImportError, FileNotFoundError), strict=True, reason="step 3 builds the signer")
nobody_else = {"acl": lambda p: [], "me": lambda: "me"}


def not_on_mainnet(_):
    return False


class Spy:
    """A fake websocket: records every message and answers like a server on `network_id`."""

    def __init__(self, network_id=1):
        self.network_id, self.urls, self.sent, self._out = network_id, [], [], []

    def __call__(self, url, **_):
        self.urls.append(url)
        return self

    def send(self, raw):
        m = json.loads(raw)
        self.sent.append(m)
        result = ({"info": {"network_id": self.network_id}} if m["command"] == "server_info"
                  else {"engine_result": "tesSUCCESS", "accepted": True, "applied": True})
        self._out.append(json.dumps({"id": m["id"], "status": "success", "result": result}))

    def recv(self, timeout=None):
        return self._out.pop(0)

    def close(self):
        pass

    def blobs(self):
        return [m for m in self.sent if m["command"] == "submit"]


@pytest.fixture
def key_file(tmp_path):
    p = tmp_path / "xrpl-testnet.key"
    p.write_text(FAKE_SEED + "\n", encoding="utf-8")
    return p


def signer_for(key_file, **kw):
    from debtlab.xrpl_market.live.signer import open_signer
    return open_signer(key_file, TESTNET, on_mainnet=kw.pop("on_mainnet", not_on_mainnet), **{**nobody_else, **kw})


def offer(s, **kw):
    args = {"side": "buy", "price": "0.08", "size": "10", "sequence": 5, "expiration": 800_000_000,
            "last_ledger": 1000, "fee_drops": 10}
    return s.create_offer(**{**args, **kw})


# --- guards: active now -------------------------------------------------------------------

@pytest.mark.parametrize("url", MAINNET_URLS)
def test_every_mainnet_endpoint_is_refused(url):
    with pytest.raises(MainnetBlocked):
        guards.check_endpoint(url)


@pytest.mark.parametrize("url", [
    "ws://testnet.xrpl-labs.com/", "wss://testnet.xrpl-labs.com.evil.example/", "wss://evil.example/testnet.xrpl-labs.com",
    "wss://testnet.xrpl-labs.com@evil.example/", "wss://user:pw@testnet.xrpl-labs.com/",
    "wss://testnet.xrpl-labs.com:8443/", "wss://s.devnet.rippletest.net:51233/", "", "not a url",
])
def test_lookalike_endpoints_are_refused(url):
    with pytest.raises(MainnetBlocked):
        guards.check_endpoint(url)


@pytest.mark.parametrize("url", [*TESTNET["endpoints"]["websocket"], *TESTNET["endpoints"]["json_rpc"],
                                 "wss://TESTNET.xrpl-labs.com/", "wss://s.altnet.rippletest.net:51233/"])
def test_testnet_endpoints_pass(url):
    assert guards.check_endpoint(url) == url


def test_the_allowlist_is_code_and_shares_nothing_with_the_mainnet_config():
    hosts = {u.split("/")[2].split(":")[0] for u in MAINNET_URLS}
    assert not hosts & guards.TESTNET_HOSTS and not hosts & {"testnet.xrpl-labs.com"}


@pytest.mark.parametrize("info", [{"info": {"network_id": 0}}, {"info": {"network_id": 2}}, {"info": {}}, {}])
def test_a_server_not_saying_testnet_is_refused(info):
    with pytest.raises(MainnetBlocked):
        guards.check_network(info)


def test_a_server_saying_testnet_passes():
    guards.check_network({"info": {"network_id": 1}})


@pytest.mark.parametrize("issuer", [MAINNET["rlusd"]["issuer"], *MAINNET["known_lookalikes"], "", "rSomethingElse"])
def test_other_issuers_are_refused(issuer):
    with pytest.raises(MainnetBlocked):
        guards.check_issuer(issuer)


def test_the_testnet_config_passes():
    guards.check_config(TESTNET)


@pytest.mark.parametrize("change", [
    {"network": "mainnet"}, {"network_id": 0}, {"endpoints": MAINNET["endpoints"]}, {"rlusd": MAINNET["rlusd"]},
])
def test_a_config_for_anywhere_else_is_refused(change):
    with pytest.raises(MainnetBlocked):
        guards.check_config({**TESTNET, **change})


def test_a_key_with_a_mainnet_account_is_refused():
    with pytest.raises(MainnetBlocked):
        guards.check_not_on_mainnet("rAny", lambda a: True)
    guards.check_not_on_mainnet("rAny", lambda a: False)


def test_when_mainnet_cant_be_checked_it_is_a_refusal_not_a_pass():
    def down(_):
        raise ConnectionError("no answer")
    with pytest.raises(ConnectionError):
        guards.check_not_on_mainnet("rAny", down)


def test_a_key_in_the_repository_is_refused(tmp_path):
    p = ROOT / "tests" / "not-a-real.key"
    with pytest.raises(KeyFileRefused, match="inside the repository"):
        guards.check_key_path(p, ROOT, **nobody_else)


def test_a_key_in_onedrive_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "OneDrive" / "a.key"
    p.parent.mkdir()
    p.write_text("x")
    with pytest.raises(KeyFileRefused, match="OneDrive"):
        guards.check_key_path(p, ROOT, **nobody_else)
    q = tmp_path / "Synced" / "a.key"
    q.parent.mkdir()
    q.write_text("x")
    monkeypatch.setenv("OneDriveConsumer", str(tmp_path / "Synced"))
    with pytest.raises(KeyFileRefused, match="OneDrive"):
        guards.check_key_path(q, ROOT, **nobody_else)


def test_the_mainnet_key_name_is_refused(tmp_path):
    p = tmp_path / "xrpl-bot.key"
    p.write_text("x")
    with pytest.raises(KeyFileRefused, match="mainnet"):
        guards.check_key_path(p, ROOT, **nobody_else)


def test_a_key_others_can_read_is_refused(tmp_path, key_file):
    with pytest.raises(KeyFileRefused, match="can be read by"):
        guards.check_key_path(key_file, ROOT, acl=lambda p: ["me", "BUILTIN\\Users"], me=lambda: "me")
    assert guards.check_key_path(key_file, ROOT, acl=lambda p: ["ME", "NT AUTHORITY\\SYSTEM"], me=lambda: "me") == key_file.resolve()


def test_a_missing_key_file_is_refused_by_name(tmp_path):
    with pytest.raises(KeyFileRefused, match="no key file"):
        guards.check_key_path(tmp_path / "none.key", ROOT, **nobody_else)


def test_a_refusal_names_the_path_never_the_contents(tmp_path, key_file):
    with pytest.raises(KeyFileRefused) as e:
        guards.check_key_path(key_file, ROOT, acl=lambda p: ["Everyone"], me=lambda: "me")
    assert FAKE_SEED not in str(e.value)


def test_read_acl_reads_the_real_file_system(key_file):
    who = guards.read_acl(key_file)
    assert isinstance(who, list)


def test_secrets_are_kept_out_of_git():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.key" in ignored
    assert "XRPL_SIGNING_KEY" not in (ROOT / ".env.example").read_text(encoding="utf-8")


# --- the signer: written before it exists ---------------------------------------------------

@needs_signer
@pytest.mark.parametrize("url", MAINNET_URLS)
def test_a_submitter_never_connects_to_mainnet(url):
    from debtlab.xrpl_market.live.signer import Submitter
    spy = Spy(network_id=0)
    with pytest.raises(MainnetBlocked):
        Submitter(url, connect=spy)
    assert spy.urls == []


@needs_signer
@pytest.mark.parametrize("network_id", [0, 2, None])
def test_an_allowed_host_on_another_network_gets_nothing_signed(key_file, network_id):
    from debtlab.xrpl_market.live.signer import Submitter
    spy = Spy(network_id=network_id)
    sub = Submitter(TESTNET["endpoints"]["websocket"][0], connect=spy)
    with pytest.raises(MainnetBlocked):
        sub.submit(offer(signer_for(key_file)))
    assert spy.blobs() == []


@needs_signer
def test_the_network_is_checked_before_every_submit_not_once(key_file):
    from debtlab.xrpl_market.live.signer import Submitter
    spy = Spy()
    sub = Submitter(TESTNET["endpoints"]["websocket"][0], connect=spy)
    s = signer_for(key_file)
    sub.submit(offer(s, sequence=5))
    assert len(spy.blobs()) == 1
    spy.network_id = 0                 # the same connection now answers as another network
    with pytest.raises(MainnetBlocked):
        sub.submit(offer(s, sequence=6))
    assert len(spy.blobs()) == 1
    assert [m["command"] for m in spy.sent] == ["server_info", "submit", "server_info"]


@needs_signer
def test_a_testnet_submit_goes_through_as_a_blob_only(key_file):
    from debtlab.xrpl_market.live.signer import Submitter
    spy = Spy()
    r = Submitter(TESTNET["endpoints"]["websocket"][0], connect=spy).submit(offer(signer_for(key_file)))
    assert r["engine_result"] == "tesSUCCESS" and spy.urls == TESTNET["endpoints"]["websocket"]
    (m,) = spy.blobs()
    assert set(m) == {"id", "command", "tx_blob"} and FAKE_SEED not in json.dumps(spy.sent)


@needs_signer
def test_the_config_is_refused_before_the_key_file_is_looked_at(tmp_path):
    missing = tmp_path / "no-such.key"
    with pytest.raises(MainnetBlocked):    # not KeyFileRefused: it never got that far
        from debtlab.xrpl_market.live.signer import open_signer
        open_signer(missing, {**TESTNET, "endpoints": MAINNET["endpoints"]}, on_mainnet=not_on_mainnet, **nobody_else)


@needs_signer
def test_a_key_with_a_mainnet_account_opens_no_signer(key_file):
    with pytest.raises(MainnetBlocked):
        signer_for(key_file, on_mainnet=lambda a: True)


@needs_signer
def test_the_mainnet_check_is_not_optional(key_file):
    from debtlab.xrpl_market.live.signer import open_signer
    with pytest.raises(TypeError):
        open_signer(key_file, TESTNET, **nobody_else)


@needs_signer
@pytest.mark.parametrize("side,gets_is_xrp", [("buy", False), ("sell", True)])
def test_every_offer_is_passive_expiring_and_on_the_verified_pair(key_file, side, gets_is_xrp):
    tx = decode(offer(signer_for(key_file), side=side).blob)
    assert tx["TransactionType"] == "OfferCreate"
    assert tx["Flags"] & 0x00010000 and tx["Expiration"] == 800_000_000 and tx["LastLedgerSequence"] == 1000
    token, xrp = (tx["TakerPays"], tx["TakerGets"]) if gets_is_xrp else (tx["TakerGets"], tx["TakerPays"])
    assert isinstance(xrp, str) and token["issuer"] == guards.TESTNET_ISSUER
    assert token["currency"] == TESTNET["rlusd"]["currency_hex"]


@needs_signer
def test_a_buy_gives_rlusd_for_xrp_at_the_price(key_file):
    tx = decode(offer(signer_for(key_file), side="buy", price="0.08", size="10").blob)
    assert tx["TakerPays"] == "10000000" and float(tx["TakerGets"]["value"]) == pytest.approx(0.8)
    tx = decode(offer(signer_for(key_file), side="sell", price="0.08", size="10").blob)
    assert tx["TakerGets"] == "10000000" and float(tx["TakerPays"]["value"]) == pytest.approx(0.8)


@needs_signer
@pytest.mark.parametrize("missing", ["expiration", "last_ledger"])
def test_an_offer_without_an_expiry_or_a_last_ledger_cant_be_signed(key_file, missing):
    with pytest.raises(ValueError):
        offer(signer_for(key_file), **{missing: None})


@needs_signer
def test_replacing_carries_the_old_offers_sequence_in_one_transaction(key_file):
    tx = decode(offer(signer_for(key_file), replaces=4).blob)
    assert tx["OfferSequence"] == 4 and tx["TransactionType"] == "OfferCreate"
    assert "OfferSequence" not in decode(offer(signer_for(key_file)).blob)


@needs_signer
def test_a_cancel_names_only_the_offer(key_file):
    tx = decode(signer_for(key_file).cancel_offer(offer_sequence=4, sequence=6, last_ledger=1000, fee_drops=10).blob)
    assert tx["TransactionType"] == "OfferCancel" and tx["OfferSequence"] == 4 and tx["LastLedgerSequence"] == 1000


@needs_signer
@pytest.mark.parametrize("kw", [{"side": "both"}, {"price": "0"}, {"size": "-1"}, {"fee_drops": 100_000}])
def test_nonsense_offers_cant_be_signed(key_file, kw):
    with pytest.raises(ValueError):
        offer(signer_for(key_file), **kw)


@needs_signer
def test_only_offers_can_be_built():
    tree = ast.parse(SIGNER_FILE.read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
             and (n.module or "").startswith("xrpl.models.transactions") for a in n.names}
    assert names <= {"OfferCreate", "OfferCreateFlag", "OfferCancel"}
    from debtlab.xrpl_market.live import signer as mod
    with pytest.raises(TypeError):
        mod._only_offers(Wallet)      # anything else is refused at the point of signing, whatever the caller


@needs_signer
def test_the_signer_module_cant_print_log_or_read_the_environment():
    tree = ast.parse(SIGNER_FILE.read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"logging", "os", "pickle", "traceback"}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"print", "eval", "exec", "vars"}


@needs_signer
def test_the_key_never_shows(key_file, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    s = signer_for(key_file)
    signed = offer(s)
    private = Wallet.from_seed(FAKE_SEED).private_key
    shown = [repr(s), str(s), str(vars(s)) if hasattr(s, "__dict__") else "", repr(signed), str(signed),
             signed.blob, caplog.text, *capsys.readouterr()]
    assert not any(FAKE_SEED in x or private in x for x in shown)
    assert s.address == Wallet.from_seed(FAKE_SEED).address


@needs_signer
def test_a_signer_cant_be_pickled_or_copied(key_file):
    s = signer_for(key_file)
    with pytest.raises(TypeError):
        pickle.dumps(s)


@needs_signer
def test_a_bad_key_file_doesnt_echo_its_contents(tmp_path):
    p = tmp_path / "bad.key"
    p.write_text("not-a-seed-CANARY", encoding="utf-8")
    with pytest.raises(KeyFileRefused) as e:
        signer_for(p)
    assert "CANARY" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


@needs_signer
def test_a_regular_key_signs_for_its_account(key_file):
    s = signer_for(key_file, account="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe")
    tx = decode(offer(s).blob)
    assert s.address == tx["Account"] == "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
