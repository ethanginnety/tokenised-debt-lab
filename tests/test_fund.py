"""Instrument C: the private-credit fund share and its reported NAV."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from debtlab.api.app import create_app
from debtlab.audit.db import connect, latest_nav, migrate, recent_events, save_nav
from debtlab.instruments import FundShare, NavReport, load_instruments
from debtlab.money import D
from debtlab.pricing.bond import FixedRateBond
from debtlab.service import fund_view, load_navs
from test_marketdata_audit_api import settings

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "instruments.json"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def fund() -> FundShare:
    return next(i for i in load_instruments(CONFIG) if isinstance(i, FundShare))


def report(nav="10.2417", as_of="2026-09-30", reported="2026-10-24T16:00:00+00:00") -> NavReport:
    return NavReport(fund_id="PCF-USD-SIM", nav_per_share=D(nav), as_of=date.fromisoformat(as_of),
                     reported_at=datetime.fromisoformat(reported), source="simulated")


def test_config_has_all_three_instruments():
    items = load_instruments(CONFIG)
    assert [type(i) for i in items] == [FixedRateBond, FixedRateBond, FundShare]
    f = fund()
    assert f.id == "PCF-USD-SIM" and f.currency == "USD"
    assert f.initial_nav.nav_per_share == D("10.2417")
    assert f.initial_nav.reported_at.tzinfo is not None  # UTC, never naive
    assert f.nav_stale_after_days == 45


@pytest.mark.parametrize("change, message", [
    ({"nav_per_share": "0"}, "positive"),
    ({"reported_at": "2026-10-24T16:00:00"}, "timezone"),
    ({"as_of": "2026-10-25"}, "before"),  # a NAV can't be reported before the date it values
])
def test_bad_nav_reports_rejected(tmp_path, change, message):
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["instruments"][2]["reported_nav"].update(change)
    path = tmp_path / "i.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_instruments(path)


def test_nav_reports_stored_once_and_latest_wins():
    conn = connect(":memory:")
    migrate(conn)
    assert save_nav(conn, report()) is True
    assert save_nav(conn, report()) is False  # same report twice = stored once
    later = report(nav="10.1980", as_of="2026-10-31", reported="2026-11-21T16:00:00+00:00")
    save_nav(conn, later)
    assert latest_nav(conn, "PCF-USD-SIM") == later
    assert latest_nav(conn, "NOPE") is None


def test_load_navs_seeds_config_report_and_audits_once():
    conn = connect(":memory:")
    migrate(conn)
    f = fund()
    navs = load_navs(conn, [f])
    load_navs(conn, [f])  # a restart doesn't duplicate
    assert navs["PCF-USD-SIM"] == f.initial_nav
    assert len(recent_events(conn, kind="nav_loaded")) == 1


def test_fund_view_shows_age_and_never_allows_quoting():
    v = fund_view(fund(), report(), NOW)
    assert v["nav_per_share"] == "10.2417"
    assert v["nav_as_of"] == "2026-09-30" and v["nav_age_days"] == 30
    assert v["days_since_reported"] == 6
    assert v["nav_status"] == "current"
    assert v["executable"] is False
    assert v["trading_allowed"] is False
    assert "not an executable price" in v["trading_gate_reason"]


def test_fund_nav_goes_stale_after_threshold():
    later = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)  # 46 days after 30 Sep
    assert fund_view(fund(), report(), later)["nav_status"] == "stale"
    assert fund_view(fund(), None, NOW)["nav_status"] == "missing"


def test_api_lists_fund_separately_from_bonds(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        o = c.get("/api/overview").json()
        assert [i["terms"]["id"] for i in o["instruments"]] == ["CENTURY-GBP-2126", "SHORT-USD-2029"]
        (f,) = o["funds"]
        assert f["terms"]["id"] == "PCF-USD-SIM" and f["nav_per_share"] == "10.2417"
        assert f["trading_allowed"] is False


def test_conflicting_nav_report_is_refused_not_dropped():
    """Same fund, as-of date and report time but a different NAV: a correction needs its own report time."""
    conn = connect(":memory:")
    migrate(conn)
    save_nav(conn, report())
    with pytest.raises(ValueError, match="conflicting NAV report"):
        save_nav(conn, report(nav="10.3000"))
    assert latest_nav(conn, "PCF-USD-SIM").nav_per_share == D("10.2417")
    restated = report(nav="10.3000", reported="2026-10-27T09:00:00+00:00")  # a proper restatement
    assert save_nav(conn, restated) is True
    assert latest_nav(conn, "PCF-USD-SIM") == restated
