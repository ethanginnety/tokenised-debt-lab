"""The prices the bot quotes from: the book without its own offers or expired ones, and the AMM with its fee.

The first testnet run showed why: with nothing else quoting, the bot's bid raised the mid that set its next
bid, and its best ask was its own expired offer, so its mid and its P&L came only from itself.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from debtlab.xrpl_market import book, store
from debtlab.xrpl_market.fx import FxRate
from debtlab.xrpl_market.paper.market import read_market
from debtlab.xrpl_market.verify import Amm, NetworkCosts
from debtlab.xrpl_market.view import open_readonly
from debtlab.xrpl_market import load_testnet_config

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import xrpl_monitor  # noqa: E402

BOT, OTHER = "rBotBotBotBotBotBotBotBotBotBot", "rOtherOtherOtherOtherOtherOther"
CLOSE = 847_000_000
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
CFG = load_testnet_config()
RLUSD = "524C555344000000000000000000000000000000"
ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"


def lv(price, owner=OTHER, expiration=None):
    return book.Level(D(price), D(100), D(price) * 100, owner, "id", expiration)


def bk(bids=(), asks=()):
    return book.Book(10, tuple(bids), tuple(asks))


def pool(price="0.08", fee="0.5"):
    return Amm(10, "rAmm", D(1000), D(1000) * D(price), D(price), D(fee))


def test_own_offers_never_move_the_mid():
    outside = bk([lv("0.0798")], [lv("0.0802")])
    with_own = bk([lv("0.07995", BOT), lv("0.0798")], [lv("0.08005", BOT), lv("0.0802")])
    plain, own = book.reference(outside, pool(), CLOSE, BOT), book.reference(with_own, pool(), CLOSE, BOT)
    assert own.mid == plain.mid == D("0.08") and (own.bid, own.ask) == (D("0.0798"), D("0.0802"))
    assert own.own_left_out == 2 and plain.own_left_out == 0


def test_the_first_runs_book_gives_a_price_that_owes_nothing_to_the_bot():
    """11:50 UTC on 4 Nov: its own bid at 0.079942, its own dead offer at 0.080054, the pool at 0.080034."""
    b = bk([lv("0.079942", BOT, CLOSE + 5)], [lv("0.080054", BOT, CLOSE - 1000)])
    r = book.reference(b, pool("0.080034", "0.501"), CLOSE, BOT)
    assert r.mid == D("0.080034") and r.own_left_out == 2


def test_another_accounts_expired_offer_is_left_out_but_a_live_one_stays():
    b = bk([lv("0.0799", OTHER, CLOSE), lv("0.0798", OTHER, CLOSE + 1)], [lv("0.0802", OTHER, CLOSE - 5), lv("0.0803")])
    r = book.reference(b, None, CLOSE)
    assert (r.bid, r.ask, r.expired_left_out) == (D("0.0798"), D("0.0803"), 2)      # at its close time it's expired


def test_the_amm_counts_with_its_fee_and_only_moves_a_price_it_beats():
    inside = book.reference(bk([lv("0.0798")], [lv("0.0802")]), pool(), CLOSE)        # band 0.0796 to 0.0804
    assert (inside.bid, inside.ask) == (D("0.0798"), D("0.0802"))
    wide = book.reference(bk([lv("0.0790")], [lv("0.0802")]), pool(), CLOSE)
    assert wide.bid == D("0.0796") and wide.ask == D("0.0802")


def test_the_amm_gives_a_price_when_a_side_is_empty():
    assert book.reference(bk([lv("0.0798")], []), pool(), CLOSE).ask == D("0.0804")
    both = book.reference(bk(), pool(), CLOSE)
    assert both.mid == D("0.08") and both.reasons == ()


def test_no_price_when_nothing_is_left_or_prices_cross():
    none = book.reference(bk(), None, CLOSE)
    assert none.mid is None and len(none.reasons) == 2
    only_own = book.reference(bk([lv("0.0799", BOT)], [lv("0.0801", BOT)]), None, CLOSE, BOT)
    assert only_own.mid is None
    crossed = book.reference(bk([lv("0.0802")], [lv("0.0801")]), None, CLOSE)
    assert crossed.mid is None and "crossed" in crossed.reasons[0]


def test_parse_book_keeps_the_expiration():
    bids = {"ledger_index": 10, "validated": True, "offers": [
        {"TakerGets": {"currency": RLUSD, "issuer": ISSUER, "value": "8"}, "TakerPays": "100000000", "Expiration": CLOSE + 7}]}
    asks = {"ledger_index": 10, "validated": True, "offers": [
        {"TakerGets": "100000000", "TakerPays": {"currency": RLUSD, "issuer": ISSUER, "value": "9"}}]}
    b = book.parse_book([bids], [asks], RLUSD)
    assert b.bids[0].expiration == CLOSE + 7 and b.asks[0].expiration is None


# --- through the monitor's database -----------------------------------------------------------

@pytest.fixture
def mon(tmp_path):
    path = tmp_path / "mon.sqlite3"
    conn = store.open_db(path)
    store.claim_network(conn, "testnet")
    store.record_fx(conn, FxRate(T0.date(), D("1.12"), "ECB"), T0)
    run = store.start_run(conn, T0)
    store.record_network(conn, NetworkCosts(1000, D(1), D("0.2"), D(10), D(10)), T0)
    return path, conn, run


def snap(conn, run, ledger, b, p=None, exclude=None, t=T0):
    store.record_snapshot(conn, run, "wss://x/", ledger, t, t + timedelta(seconds=1), "top", b, p, exclude)
    return t + timedelta(seconds=2)


def test_the_market_reads_the_reference_and_the_raw_book_stays_on_record(mon):
    path, conn, run = mon
    now = snap(conn, run, 10, bk([lv("0.07995", BOT), lv("0.0798")], [lv("0.0802")]), pool(), BOT)
    m = read_market(open_readonly(path), now, CFG)
    assert (m.best_bid, m.best_ask, m.mid) == (D("0.0798"), D("0.0802"), D("0.08"))
    assert m.valid and m.excluded_account == BOT
    row = conn.execute("SELECT best_bid, own_left_out, excluded_account FROM xrpl_snapshots").fetchone()
    assert D(row["best_bid"]) == D("0.07995") and row["own_left_out"] == 1 and row["excluded_account"] == BOT


def test_volatility_is_measured_on_the_reference_so_our_quotes_add_none(mon):
    path, conn, run = mon
    t = T0
    for i in range(6):
        own = [lv("0.07995", BOT)] if i % 2 else []
        t = snap(conn, run, 10 + i, bk([*own, lv("0.0798")], [lv("0.0802")]), None, BOT, t)
    assert set(read_market(open_readonly(path), t, CFG).mids) == {D("0.08")}


def test_a_book_invalid_only_because_of_our_dead_offer_is_usable(mon):
    path, conn, run = mon
    b = bk([lv("0.0798")], [lv("0.0797", BOT, CLOSE - 100), lv("0.0802")])     # the raw book is crossed
    now = snap(conn, run, 10, b, None, BOT)
    m = read_market(open_readonly(path), now, CFG)
    assert m.valid and m.mid == D("0.08")


def test_no_price_is_invalid_with_the_reason(mon):
    path, conn, run = mon
    now = snap(conn, run, 10, bk([lv("0.0799", BOT)], [lv("0.0801", BOT)]), None, BOT)
    m = read_market(open_readonly(path), now, CFG)
    assert not m.valid and m.mid is None and any("book invalid" in p for p in m.problems(D(30)))


def _before_the_reference(path):
    c = sqlite3.connect(path)
    for col in ("ref_bid", "ref_ask", "ref_mid", "ref_reasons", "excluded_account", "own_left_out", "expired_left_out"):
        c.execute(f"ALTER TABLE xrpl_snapshots DROP COLUMN {col}")
    c.commit()
    c.close()


def test_a_database_not_yet_migrated_reads_as_the_raw_book(mon):
    """The mainnet monitor's, until it's restarted after the checkpoint."""
    path, conn, run = mon
    now = snap(conn, run, 10, bk([lv("0.0798")], [lv("0.0802")]), None)
    conn.close()
    _before_the_reference(path)
    m = read_market(open_readonly(path), now, CFG)
    assert m.mid == D("0.08") and m.valid and m.excluded_account is None


def test_rows_from_before_the_migration_still_count(mon):
    path, conn, run = mon
    t = snap(conn, run, 10, bk([lv("0.0798")], [lv("0.0802")]), None)
    conn.execute("UPDATE xrpl_snapshots SET ref_bid = NULL, ref_ask = NULL, ref_mid = NULL, ref_reasons = NULL")
    conn.commit()
    t = snap(conn, run, 11, bk([lv("0.0800")], [lv("0.0804")]), None, t=t)
    assert read_market(open_readonly(path), t, CFG).mids == (D("0.08"), D("0.0802"))


def test_monitor_status_never_migrates_or_writes_to_the_database(mon, capsys):
    """A monitor may be writing to it: the mainnet one must be left alone until the checkpoint."""
    path, conn, run = mon
    snap(conn, run, 10, bk([lv("0.0798")], [lv("0.0802")]), None)
    conn.close()
    _before_the_reference(path)
    assert xrpl_monitor.main(["status", "--db", str(path), "--network", "testnet"]) == 0
    c = sqlite3.connect(path)
    assert "ref_mid" not in [r[1] for r in c.execute("PRAGMA table_info(xrpl_snapshots)")]
    assert "RLUSD/XRP monitor" in capsys.readouterr().out


def test_monitor_status_still_refuses_the_wrong_network(mon, capsys):
    path, conn, run = mon
    conn.close()
    assert xrpl_monitor.main(["status", "--db", str(path)]) == 2
    assert "records testnet" in capsys.readouterr().out


def test_monitor_refuses_something_that_isnt_an_address(tmp_path, capsys):
    assert xrpl_monitor.main(["run", "--db", str(tmp_path / "m.sqlite3"), "--network", "testnet", "--for", "0",
                              "--exclude-account", "not-an-address"]) == 2
    assert "isn't an XRPL address" in capsys.readouterr().out
    assert not (tmp_path / "m.sqlite3").exists()
