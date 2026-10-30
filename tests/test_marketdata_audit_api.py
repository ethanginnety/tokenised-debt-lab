"""Market data parsing/staleness, the audit log, and the web API."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from debtlab.api.app import create_app, render_page
from debtlab.audit.db import MIGRATIONS_DIR, connect, latest_curve, migrate, record_event, recent_events, save_curve
from debtlab.config import Settings
from debtlab.marketdata import adapters
from debtlab.marketdata.adapters import MarketDataError, parse_ecb_csv, parse_treasury_csv
from debtlab.marketdata.models import MARKET_KINDS, CurveSnapshot, risk_increasing_allowed
from debtlab.money import D
from debtlab.service import load_curves

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def settings(tmp_path, mode="offline", **kw):
    base = dict(
        db_path=tmp_path / "t.sqlite3", data_mode=mode, max_curve_age_hours=96,
        settlement_date=date(2026, 10, 29),
        instruments_file=Path(__file__).parent.parent / "config" / "instruments.json",
        host="127.0.0.1", port=8000,
    )
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------- parsing


def test_treasury_csv_uses_latest_row_and_skips_blanks():
    snap = parse_treasury_csv((FIX / "treasury_sample.csv").read_text(encoding="utf-8"), NOW)
    assert snap.observation_date == date(2026, 10, 27)
    assert snap.rate_type == "par" and snap.is_market_data
    tenors = [t for t, _ in snap.curve.points]
    assert D(1) / D(12) in tenors and D(30) in tenors
    assert D("1.5") / D(12) not in tenors  # blank cell skipped, not invented
    assert snap.curve.zero_rate(D(10)) == D("0.0421")


def test_treasury_garbage_raises_instead_of_inventing():
    with pytest.raises(MarketDataError):
        parse_treasury_csv("Date,1 Mo\nnot-a-date,4.0\n", NOW)
    with pytest.raises(MarketDataError):
        parse_treasury_csv("", NOW)


def test_ecb_parse_and_mixed_dates_rejected():
    text = lambda d, v: f"KEY,TIME_PERIOD,OBS_VALUE\nYC.X,{d},{v}\n"  # noqa: E731
    snap = parse_ecb_csv({"1Y": text("2026-10-27", "2.1"), "10Y": text("2026-10-27", "2.9")}, NOW)
    assert snap.curve.zero_rate(D(10)) == D("0.029") and snap.rate_type == "zero"
    with pytest.raises(MarketDataError):
        parse_ecb_csv({"1Y": text("2026-10-27", "2.1"), "10Y": text("2026-10-26", "2.9")}, NOW)


def test_retries_then_fails_cleanly():
    calls = []

    def boom():
        calls.append(1)
        raise ConnectionError("down")

    with pytest.raises(MarketDataError):
        adapters.with_retries(boom, attempts=3, base_delay=0)
    assert len(calls) == 3


# ---------------------------------------------------------------- staleness gate


def test_staleness_and_risk_gate():
    snap = parse_treasury_csv((FIX / "treasury_sample.csv").read_text(encoding="utf-8"), NOW)
    assert snap.status(NOW, 96) == "fresh"
    later = NOW + timedelta(days=5)
    assert snap.status(later, 96) == "stale"
    assert risk_increasing_allowed(snap, NOW, 96) == (True, "ok")
    assert risk_increasing_allowed(snap, later, 96)[0] is False
    illus = adapters.illustrative_curves(NOW)["GBP"]
    assert risk_increasing_allowed(illus, NOW, 96)[0] is False
    assert risk_increasing_allowed(None, NOW, 96)[0] is False


# ---------------------------------------------------------------- audit log


def test_migrations_idempotent_and_events_survive_restart(tmp_path):
    db = tmp_path / "a.sqlite3"
    conn = connect(db)
    every = sorted(f.name for f in MIGRATIONS_DIR.glob("*.sql"))
    assert every[0] == "001_init.sql" and migrate(conn) == every  # all, in order
    assert migrate(conn) == []
    assert record_event(conn, "valuation", {"x": 1}, entity="B", idempotency_key="k1") is True
    assert record_event(conn, "valuation", {"x": 1}, entity="B", idempotency_key="k1") is False
    conn.close()
    conn2 = connect(db)  # "restart"
    assert migrate(conn2) == []
    assert len(recent_events(conn2, kind="valuation")) == 1


def test_last_valid_curve_kept_when_source_fails(tmp_path, monkeypatch):
    s = settings(tmp_path, mode="online")
    conn = connect(s.db_path)
    migrate(conn)
    old = parse_treasury_csv((FIX / "treasury_sample.csv").read_text(encoding="utf-8"), NOW)
    save_curve(conn, old)

    def fail(_now):
        raise MarketDataError("source down")

    monkeypatch.setattr(adapters, "ONLINE_ADAPTERS", {"USD": fail, "GBP": fail})
    import debtlab.service as service
    monkeypatch.setattr(service, "ONLINE_ADAPTERS", {"USD": fail, "GBP": fail})
    later = NOW + timedelta(days=6)
    curves = load_curves(conn, s, later)
    from debtlab.marketdata.adapters import to_zero_snapshot
    assert curves["USD"].id == to_zero_snapshot(old).id  # the stored observation, bootstrapped
    assert curves["USD"].status(later, 96) == "stale"  # ...and it is flagged stale
    assert "GBP" not in curves  # nothing stored, so nothing invented
    assert all(c.kind in MARKET_KINDS for c in curves.values())
    errors = recent_events(conn, kind="marketdata_error")
    assert {e["entity"] for e in errors} == {"USD", "GBP"}


def test_snapshot_json_round_trip():
    snap = adapters.illustrative_curves(NOW)["GBP"]
    back = CurveSnapshot.from_json(snap.to_json())
    assert back.curve.points == snap.curve.points and back.kind == snap.kind


# ---------------------------------------------------------------- API


def test_api_offline_end_to_end(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        h = c.get("/api/health").json()
        assert h["status"] == "ok" and h["curves"] == {"GBP": "illustrative", "USD": "illustrative"}
        o = c.get("/api/overview").json()
        ids = {i["terms"]["id"]: i for i in o["instruments"]}
        century = ids["CENTURY-GBP-2126"]
        assert century["status"] == "ok"
        assert century["trading_allowed"] is False  # illustrative data never drives quoting
        assert D("10") < D(century["modified_duration"]) < D("17")
        assert D(ids["SHORT-USD-2029"]["modified_duration"]) < D(3)
        # Illustrative curves compound at each bond's own frequency.
        assert [century["curve_compounding"], ids["SHORT-USD-2029"]["curve_compounding"]] == [1, 2]
        assert all(cv["kind"] == "illustrative" for cv in o["curves"])
        kinds = {e["kind"] for e in o["events"]}
        assert {"app_started", "curve_loaded", "valuation"} <= kinds
        # Refreshing on identical data and date does not duplicate valuation events.
        before = len(c.get("/api/events", params={"kind": "valuation"}).json())
        c.post("/api/refresh")
        assert len(c.get("/api/events", params={"kind": "valuation"}).json()) == before
        page = c.get("/")
        assert page.status_code == 200 and "<title>Tokenised Debt Lab</title>" in page.text


def test_online_without_gbp_source_leaves_century_unpriced(tmp_path, monkeypatch):
    import debtlab.service as service

    def fail(_now):
        raise MarketDataError("unreachable")

    monkeypatch.setattr(service, "ONLINE_ADAPTERS", {"USD": fail, "GBP": fail})
    with TestClient(create_app(settings(tmp_path, mode="online"))) as c:
        o = c.get("/api/overview").json()
        assert all(i["status"] == "no_data" for i in o["instruments"])
        assert o["curves"] == []


def test_snapshot_export_escapes_script_tags():
    html = render_page('{"x": "</script><b>"}', shell=False)
    assert "window.__SNAPSHOT__" in html and "</script><b>" not in html.split("window.__SNAPSHOT__")[1][:40]


def test_reprocessed_curve_is_recorded_again(tmp_path):
    """Same source and date but different content (par vs bootstrapped zero)
    must get a new id, so the new curve and the new valuation are both logged."""
    from debtlab.marketdata.adapters import to_zero_snapshot

    conn = connect(tmp_path / "r.sqlite3")
    migrate(conn)
    par = parse_treasury_csv((FIX / "treasury_sample.csv").read_text(encoding="utf-8"), NOW)
    zero = to_zero_snapshot(par)
    assert par.id != zero.id
    assert par.id.rsplit(":", 1)[0] == zero.id.rsplit(":", 1)[0]  # same source and date
    assert save_curve(conn, par) and save_curve(conn, zero)
    assert latest_curve(conn, "USD", MARKET_KINDS) is not None


def test_fallback_par_curve_is_bootstrapped(tmp_path, monkeypatch):
    import debtlab.service as service

    s = settings(tmp_path, mode="online")
    conn = connect(s.db_path)
    migrate(conn)
    save_curve(conn, parse_treasury_csv((FIX / "treasury_sample.csv").read_text(encoding="utf-8"), NOW))

    def fail(_now):
        raise MarketDataError("down")

    monkeypatch.setattr(service, "ONLINE_ADAPTERS", {"USD": fail})
    curves = load_curves(conn, s, NOW)
    assert curves["USD"].rate_type == "zero_bootstrapped" and curves["USD"].curve.compounding == 2


# ---------------------------------------------------------------- review fixes (30 Oct 2026)


@pytest.mark.parametrize("error", [
    __import__("decimal").InvalidOperation("D('N/A')"),  # non-numeric cell
    KeyError("TIME_PERIOD"),  # missing column
    ArithmeticError("non-positive discount factor"),  # bootstrap failure
])
def test_any_source_failure_falls_back_and_spares_other_currencies(tmp_path, monkeypatch, error):
    import debtlab.service as service

    s = settings(tmp_path, mode="online")
    conn = connect(s.db_path)
    migrate(conn)
    old = parse_treasury_csv((FIX / "treasury_sample.csv").read_text(encoding="utf-8"), NOW)
    save_curve(conn, old)
    gbp = adapters.illustrative_curves(NOW)["GBP"]

    def broken(_now):
        raise error

    monkeypatch.setattr(service, "ONLINE_ADAPTERS", {"USD": broken, "GBP": lambda _now: gbp})
    curves = load_curves(conn, s, NOW)
    assert curves["USD"].id == adapters.to_zero_snapshot(old).id  # last stored curve, not a crash
    assert curves["GBP"] is gbp  # the currency that loaded is kept
    (err,) = recent_events(conn, kind="marketdata_error")
    assert err["entity"] == "USD" and err["payload"]["error_type"] == type(error).__name__


@pytest.mark.parametrize("settle, unpriced, reason", [
    (date(2026, 3, 1), "CENTURY-GBP-2126", "not issued yet"),  # before 30 Mar 2026
    (date(2029, 3, 1), "SHORT-USD-2029", "matured"),  # after 17 Feb 2029
])
def test_bond_outside_its_life_is_skipped_not_fatal(tmp_path, settle, unpriced, reason):
    with TestClient(create_app(settings(tmp_path, settlement_date=settle))) as c:
        ids = {i["terms"]["id"]: i for i in c.get("/api/overview").json()["instruments"]}
        assert ids[unpriced]["status"] == "not_priced" and reason in ids[unpriced]["message"]
        (other,) = [v for k, v in ids.items() if k != unpriced]
        assert other["status"] == "ok"  # the other bond is still priced
        skipped = c.get("/api/events", params={"kind": "valuation_skipped"}).json()
        assert [e["entity"] for e in skipped] == [unpriced]


def test_quoting_gate_is_reapplied_when_a_curve_ages(tmp_path, monkeypatch):
    import debtlab.service as service

    usd = adapters.to_zero_snapshot(
        parse_treasury_csv((FIX / "treasury_sample.csv").read_text(encoding="utf-8"), NOW))

    def down(_now):
        raise MarketDataError("down")

    monkeypatch.setattr(service, "ONLINE_ADAPTERS", {"USD": lambda _now: usd, "GBP": down})
    clock = [NOW]  # observed 27 Oct, so 60h old: fresh
    with TestClient(create_app(settings(tmp_path, mode="online"), clock=lambda: clock[0])) as c:
        short = lambda: next(i for i in c.get("/api/overview").json()["instruments"]  # noqa: E731
                             if i["terms"]["id"] == "SHORT-USD-2029")
        assert short()["trading_allowed"] is True and short()["curve_status"] == "fresh"
        clock[0] = NOW + timedelta(days=3)  # 132h: stale, and nobody pressed Refresh
        later = short()
        assert later["trading_allowed"] is False and later["curve_status"] == "stale"
        assert "stale" in later["trading_gate_reason"]


def test_changed_bond_terms_on_same_curve_are_recorded(tmp_path):
    from dataclasses import replace

    from debtlab.instruments import load_instruments
    from debtlab.pricing.bond import FixedRateBond
    from debtlab.service import value_all

    s = settings(tmp_path)
    conn = connect(s.db_path)
    migrate(conn)
    curves = load_curves(conn, s, NOW)
    short = next(b for b in load_instruments(s.instruments_file)
                 if isinstance(b, FixedRateBond) and b.currency == "USD")
    value_all(conn, [short], curves, date(2026, 10, 29), s, NOW)
    value_all(conn, [short], curves, date(2026, 10, 29), s, NOW)  # identical: recorded once
    assert len(recent_events(conn, kind="valuation")) == 1
    wider = replace(short, credit_spread=short.credit_spread + D("0.005"))  # config edited
    value_all(conn, [wider], curves, date(2026, 10, 29), s, NOW)
    events = recent_events(conn, kind="valuation")
    assert len(events) == 2 and events[0]["payload"]["fair_clean"] != events[1]["payload"]["fair_clean"]


@pytest.mark.parametrize("argv, out, full", [
    ([], "snapshot.html", False),
    (["--full"], "snapshot.html", True),  # used to write a file named "--full"
    (["--full", "x.html"], "x.html", True),
    (["x.html", "--full"], "x.html", True),
])
def test_export_snapshot_arguments(argv, out, full):
    import importlib.util

    path = Path(__file__).parent.parent / "scripts" / "export_snapshot.py"
    spec = importlib.util.spec_from_file_location("export_snapshot", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.parse_args(argv) == (Path(out), full)
