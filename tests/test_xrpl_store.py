"""The monitor's database (M7 step 3): runs, outages, uptime, snapshots, trades, decision C."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from debtlab.audit.db import connect, migrate
from debtlab.xrpl_market import book, fx, load_config, store, verify
from debtlab.xrpl_market.trades import Trade

CFG = load_config()
RLUSD, ISSUER = CFG["rlusd"]["currency_hex"], CFG["rlusd"]["issuer"]
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def s(n):
    return T0 + timedelta(seconds=n)


@pytest.fixture
def conn(tmp_path):
    c = store.open_db(tmp_path / "xrpl.sqlite3")
    yield c
    c.close()


def rl(v):
    return {"currency": RLUSD, "issuer": ISSUER, "value": str(v)}


def mkbook(ledger, bid=Decimal("1.249"), ask=Decimal("1.251"), xrp=100, empty_asks=False, n=1):
    def o(p, side):
        if side == "bid":
            return {"TakerGets": rl(p * xrp), "TakerPays": str(xrp * 1_000_000)}
        return {"TakerGets": str(xrp * 1_000_000), "TakerPays": rl(p * xrp)}
    bids = {"ledger_index": ledger, "validated": True, "offers": [o(bid - Decimal("0.001") * i, "bid") for i in range(n)]}
    asks = {"ledger_index": ledger, "validated": True,
            "offers": [] if empty_asks else [o(ask + Decimal("0.001") * i, "ask") for i in range(n)]}
    return book.parse_book([bids], [asks], RLUSD)


POOL = verify.Amm(1, "rAMM", Decimal(1000), Decimal(1250), Decimal("1.25"), Decimal("0.214"))


def snap(conn, run, ledger, at, **kw):
    kind = kw.pop("kind", "top")
    return store.record_snapshot(conn, run, "wss://test", ledger, at - timedelta(seconds=2), at, kind,
                                 mkbook(ledger, **kw), POOL)


def outages(conn):
    return [dict(r) for r in conn.execute("SELECT kind, started_at, ended_at, detail FROM xrpl_outages ORDER BY id")]


def test_refuses_a_bond_database(tmp_path):
    c = connect(tmp_path / "bond.sqlite3")
    migrate(c)
    c.close()
    with pytest.raises(store.WrongDatabase):
        store.open_db(tmp_path / "bond.sqlite3")


def test_naive_times_refused(conn):
    with pytest.raises(ValueError):
        store.start_run(conn, datetime(2026, 11, 1, 12, 0))


def test_time_not_running_is_an_outage_on_the_next_start(conn):
    r1 = store.start_run(conn, s(0))
    snap(conn, r1, 10, s(4))
    store.open_outage(conn, "disconnected", s(10), "test")     # then the process dies: no clean stop
    r2 = store.start_run(conn, s(600))
    o = outages(conn)
    assert [x["kind"] for x in o] == ["disconnected", "not_running"]
    assert o[0]["ended_at"] == store.iso(s(10))                 # open outage ends at the last heartbeat
    assert (o[1]["started_at"], o[1]["ended_at"]) == (store.iso(s(10)), store.iso(s(600)))
    run1 = conn.execute("SELECT * FROM xrpl_runs WHERE id = ?", (r1,)).fetchone()
    assert run1["stopped_at"] == store.iso(s(10)) and "no clean stop" in run1["stop_reason"]
    assert r2 == r1 + 1


def test_quick_restart_is_not_an_outage(conn):
    r1 = store.start_run(conn, s(0))
    store.stop_run(conn, r1, s(100), "stopped by user")
    store.start_run(conn, s(110))
    assert outages(conn) == []


def test_one_open_outage_at_a_time(conn):
    r = store.start_run(conn, s(0))
    store.open_outage(conn, "stale", s(40), "no ledger")
    store.open_outage(conn, "stale", s(50), "again")            # same kind: nothing new
    store.open_outage(conn, "disconnected", s(60), "closed")    # another kind: the first ends here
    snap(conn, r, 20, s(90))                                     # fresh data closes it
    o = outages(conn)
    assert [(x["kind"], x["ended_at"]) for x in o] == [("stale", store.iso(s(60))), ("disconnected", store.iso(s(90)))]


def test_missed_ledgers_recorded(conn):
    r = store.start_run(conn, s(0))
    snap(conn, r, 10, s(4))
    snap(conn, r, 11, s(8))
    snap(conn, r, 15, s(24))
    (o,) = outages(conn)
    assert o["kind"] == "missed_ledgers" and "3 ledger(s) not read: 12..14" in o["detail"]
    assert (o["started_at"], o["ended_at"]) == (store.iso(s(8)), store.iso(s(24)))


def test_uptime_from_the_data(conn):
    r = store.start_run(conn, s(0))
    for i, t in enumerate([4, 8, 12, 16]):          # 12 s covered
        snap(conn, r, 10 + i, s(t))
    for i, t in enumerate([100, 104]):               # a 84 s gap: down
        snap(conn, r, 20 + i, s(t))
    u = store.uptime(conn, s(110))                   # + 4 between, + 6 tail
    assert u.span_s == 110 and u.up_s == 12 + 4 + 6
    late = store.uptime(conn, s(200))                # tail capped at 30 s
    assert late.up_s == 12 + 4 + 30
    window = store.uptime(conn, s(110), since=s(100))
    assert window.span_s == 10 and window.up_s == 4 + 6


def test_snapshot_stored_with_its_figures(conn):
    r = store.start_run(conn, s(0))
    st = snap(conn, r, 10, s(4), kind="deep")
    row = conn.execute("SELECT * FROM xrpl_snapshots").fetchone()
    assert st.valid and row["valid"] == 1 and row["kind"] == "deep"
    assert (Decimal(row["best_bid"]), Decimal(row["best_ask"]), Decimal(row["spread_bp"])) == \
        (Decimal("1.249"), Decimal("1.251"), Decimal("16"))
    assert json.loads(row["levels_json"])["bids"][0] == ["1.249", "100"]
    assert {d["band_bp"] for d in json.loads(row["depth_json"])} == {10, 25, 50, 100}
    assert row["amm_fee_pct"] == "0.214"
    hb = conn.execute("SELECT * FROM xrpl_heartbeat").fetchone()
    assert (hb["at"], hb["ledger_index"]) == (store.iso(s(4)), 10)


def test_invalid_snapshot_kept_with_reasons(conn):
    r = store.start_run(conn, s(0))
    snap(conn, r, 10, s(4), empty_asks=True)
    row = conn.execute("SELECT valid, reasons, mid FROM xrpl_snapshots").fetchone()
    assert row["valid"] == 0 and "no funded asks" in row["reasons"] and row["mid"] is None


def test_trades_kept_once(conn):
    t = Trade("H" * 64, "OFF1", 10, "book", "sell_xrp", Decimal("1.25"), Decimal(40), Decimal(50), "rM", "OfferCreate")
    assert store.record_trades(conn, [t], s(1)) == 1
    assert store.record_trades(conn, [t], s(2)) == 0          # the same trade from the second subscription
    assert conn.execute("SELECT COUNT(*) FROM xrpl_trades").fetchone()[0] == 1


def test_issuer_change_is_an_event(conn):
    base = verify.IssuerSettings(ISSUER, 10, "https://ripple.com/", {"allowTrustLineClawback": True}, Decimal(0), None)
    assert not store.record_issuer(conn, base, s(0))
    assert not store.record_issuer(conn, verify.IssuerSettings(ISSUER, 20, base.domain, base.flags, Decimal(0), None), s(1))
    assert store.record_issuer(conn, verify.IssuerSettings(ISSUER, 30, base.domain, base.flags, Decimal(0), 5), s(2))
    ev = conn.execute("SELECT kind FROM xrpl_events").fetchall()
    assert [e["kind"] for e in ev] == ["issuer_changed"]


def test_fx_latest_and_thinning(conn):
    store.record_fx(conn, fx.FxRate(date(2026, 10, 29), Decimal("1.113"), "ECB"), s(0))
    store.record_fx(conn, fx.FxRate(date(2026, 10, 30), Decimal("1.1172"), "ECB"), s(0))
    assert store.latest_fx(conn).usd_per_eur == Decimal("1.1172")
    r = store.start_run(conn, s(0))
    snap(conn, r, 10, s(4))
    assert store.thin(conn, s(4) + timedelta(days=2)) == 0
    assert store.thin(conn, s(4) + timedelta(days=4)) == 1
    assert conn.execute("SELECT levels_json, best_bid FROM xrpl_snapshots").fetchone()[:] == (None, "1.249")


def test_decision_c(conn):
    store.record_fx(conn, fx.FxRate(date(2026, 10, 30), Decimal("1.25"), "ECB"), s(0))
    r = store.start_run(conn, s(0))
    for i in range(99):
        snap(conn, r, 10 + i, s(4 * i + 1), kind="deep" if i % 10 == 0 else "top", n=3)
    snap(conn, r, 200, s(500), empty_asks=True)                 # 1 of 100 ledgers one-sided
    c = store.depth_check(conn)
    assert c.ledgers_read == 100 and c.both_sides_share == 0.99
    assert c.median_spread_bp == Decimal("16") and c.deep_reads == 10
    # within 50 bp (1.24375..1.25625): bids 1.249, 1.248, 1.247 and asks 1.251, 1.252, 1.253, 100 XRP each
    assert c.median_bid_eur_50bp == (Decimal("124.9") + Decimal("124.8") + Decimal("124.7")) / Decimal("1.25")
    assert all(c.passes.values())
    snap(conn, r, 201, s(504), empty_asks=True)
    assert store.depth_check(conn).passes["both sides funded in >= 99% of ledgers"] is False


def test_decision_c_needs_a_rate_for_euros(conn):
    r = store.start_run(conn, s(0))
    snap(conn, r, 10, s(1), kind="deep")
    c = store.depth_check(conn)
    assert c.median_bid_eur_50bp is None and c.passes["median depth within 50 bp >= EUR 100 each side"] is None
