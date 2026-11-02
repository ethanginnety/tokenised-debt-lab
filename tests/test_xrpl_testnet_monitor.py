"""M8 step 4: the testnet monitor can't touch the mainnet evidence. Nothing reaches a network."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from paperworld import mkbook

from debtlab.xrpl_market import load_config, load_testnet_config, store
from debtlab.xrpl_market.live import guards

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import xrpl_monitor  # noqa: E402

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def with_data(path):
    conn = store.open_db(path)
    run = store.start_run(conn, T0)
    store.record_snapshot(conn, run, "wss://x", 1, T0, T0, "top", mkbook(1, "1.249", "1.251"), None)
    return conn


def test_a_database_with_unlabelled_data_is_mainnet(tmp_path):
    assert store.network(with_data(tmp_path / "m.sqlite3")) == "mainnet"


def test_an_empty_database_has_no_network_until_claimed(tmp_path):
    conn = store.open_db(tmp_path / "e.sqlite3")
    assert store.network(conn) is None
    store.claim_network(conn, "testnet")
    assert store.network(conn) == "testnet"


def test_the_mainnet_evidence_refuses_testnet(tmp_path):
    with pytest.raises(store.WrongDatabase, match="records mainnet"):
        store.claim_network(with_data(tmp_path / "m.sqlite3"), "testnet")


def test_a_testnet_database_refuses_mainnet(tmp_path):
    conn = store.open_db(tmp_path / "t.sqlite3")
    store.claim_network(conn, "testnet")
    with pytest.raises(store.WrongDatabase, match="records testnet"):
        store.claim_network(conn, "mainnet")


def test_claiming_the_same_network_again_is_fine(tmp_path):
    conn = with_data(tmp_path / "m.sqlite3")
    store.claim_network(conn, "mainnet")
    store.claim_network(conn, "mainnet")
    assert store.network(conn) == "mainnet"


def test_the_script_refuses_before_connecting(tmp_path, capsys):
    db = tmp_path / "m.sqlite3"
    with_data(db).close()
    assert xrpl_monitor.main(["run", "--network", "testnet", "--db", str(db), "--for", "0"]) == 2
    assert "records mainnet" in capsys.readouterr().out


def test_testnet_ignores_the_mainnet_monitors_environment_setting(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite3"
    with_data(db).close()
    monkeypatch.setenv("DEBTLAB_XRPL_DB", str(db))
    opened = []
    real = store.open_db
    monkeypatch.setattr(xrpl_monitor.store, "open_db", lambda p: (opened.append(Path(p)), real(tmp_path / "t.sqlite3"))[1])
    assert xrpl_monitor.main(["status", "--network", "testnet"]) == 0
    assert opened == [xrpl_monitor.ROOT / "data" / "xrpl_testnet.sqlite3"]


def test_the_default_is_still_mainnet(monkeypatch, tmp_path, capsys):
    db = tmp_path / "x.sqlite3"
    assert xrpl_monitor.main(["status", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "TESTNET" not in out and store.network(store.open_db(db)) == "mainnet"


def test_the_testnet_config_passes_the_guards_and_shares_the_one_strategy():
    cfg, main = load_testnet_config(), load_config()
    guards.check_config(cfg)
    assert cfg["paper"] == main["paper"] and cfg["monitor"] == main["monitor"]
    assert cfg["rlusd"]["issuer"] != main["rlusd"]["issuer"]
    assert main["rlusd"]["issuer"] in cfg["known_lookalikes"]


def test_the_monitor_module_stays_read_only_on_testnet():
    from debtlab.xrpl_market.monitor import Monitor
    from debtlab.xrpl_market.wsclient import ReadOnlyWebsocket
    assert "submit" not in Monitor.__dict__ and "submit" not in ReadOnlyWebsocket.__dict__
