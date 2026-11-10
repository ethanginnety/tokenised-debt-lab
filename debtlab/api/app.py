"""Web app: JSON API plus the dashboard page.

Run with:  python -m debtlab.api   (see README.md)
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response

from debtlab import ledger
from debtlab.audit.db import atomic, connect, get_db_role, migrate, record_event, recent_events, set_db_role
from debtlab.config import ROOT, Settings
from debtlab.instruments import FundShare, load_instruments
from debtlab.marketmaker.book import load_book_config, seed_book
from debtlab.marketmaker.controls import seed_limits
from debtlab.pricing.bond import FixedRateBond
from debtlab.xrpl_market.liveview import live_view
from debtlab.xrpl_market.view import xrpl_view
from debtlab.service import (
    curve_view,
    fund_view,
    lifecycle_summary,
    lifecycle_view,
    load_curves,
    load_navs,
    marketmaker_view,
    value_all,
    with_current_gate,
)

STATIC = Path(__file__).resolve().parent / "static"
VERSION = "0.3.0-dev"

_SHELL = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
    "</head><body>{body}</body></html>"
)


def render_page(snapshot_json: str | None = None, shell: bool = True) -> str:
    """The dashboard page. With `snapshot_json`, data is embedded (static export)."""
    body = (STATIC / "index.html").read_text(encoding="utf-8")
    if snapshot_json is not None:
        safe = snapshot_json.replace("</", "<\\/")
        body = body.replace("<script>", f"<script>window.__SNAPSHOT__ = {safe};</script>\n<script>", 1)
    return _SHELL.format(body=body) if shell else body


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _display_path(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


class State:
    def __init__(self, settings: Settings, clock: Callable[[], datetime] = utc_now) -> None:
        self.settings = settings
        self.clock = clock  # tests substitute a fake clock
        self.lock = threading.Lock()
        self.conn = connect(settings.db_path)
        migrate(self.conn)
        if get_db_role(self.conn) is None:
            set_db_role(self.conn, "main")  # scenario databases are only made by scripts/new_scenario_db.py
        self.db_role = get_db_role(self.conn)
        instruments = load_instruments(settings.instruments_file)
        # Test holders and their starting positions (idempotent). Main is seeded too but never advanced.
        ledger.seed(self.conn, ledger.load_holders_config(settings.holders_file), instruments)
        if self.db_role == "main":  # a scenario database gets the market maker's book only when created with it
            seed_book(self.conn, load_book_config(settings.marketmaker_file), instruments)
            seed_limits(self.conn, settings.marketmaker_file)  # first time only: the database then rules
        self.bonds = [i for i in instruments if isinstance(i, FixedRateBond)]
        self.funds = [i for i in instruments if isinstance(i, FundShare)]
        self.curves: dict = {}
        self.source_failures: list[dict] = []  # official sources that failed on the last refresh
        self.navs: dict = {}
        self.valuations: list[dict] = []
        self.last_refresh: datetime | None = None
        self.started_at = clock()

    def settle_date(self):
        return self.settings.settlement_date or self.clock().date()

    def refresh(self) -> None:
        with self.lock:
            now = self.clock()
            failures: list[dict] = []
            self.curves = load_curves(self.conn, self.settings, now, failures)
            self.source_failures = failures
            self.navs = load_navs(self.conn, self.funds)
            self.valuations = value_all(
                self.conn, self.bonds, self.curves, self.settle_date(), self.settings, now
            )
            self.last_refresh = now


def create_app(settings: Settings | None = None, clock: Callable[[], datetime] = utc_now) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = State(settings, clock)
        record_event(state.conn, "app_started",
                     {"version": VERSION, "data_mode": settings.data_mode})
        state.refresh()
        app.state.s = state
        yield
        record_event(state.conn, "app_stopped", {"version": VERSION})
        state.conn.close()

    app = FastAPI(title="Tokenised Debt Lab", version=VERSION, lifespan=lifespan)

    def read_lifecycle(build: Callable[[State], dict]) -> dict:
        """Lifecycle reads in one snapshot, so a scripts/lifecycle.py run part-way through isn't half-shown."""
        s: State = app.state.s
        with s.lock, atomic(s.conn):  # an outer savepoint is a read transaction; nothing is written
            return build(s)

    def overview() -> dict:
        s: State = app.state.s
        now = s.clock()
        return {
            "generated_at": now.isoformat(),
            "version": VERSION,
            "mode": "simulation",
            "database": {"role": s.db_role, "path": _display_path(settings.db_path)},
            "data_mode": settings.data_mode,
            "settle": s.settle_date().isoformat(),
            "last_refresh": s.last_refresh.isoformat() if s.last_refresh else None,
            "curves": [curve_view(c, now, settings.max_curve_age_hours) for c in s.curves.values()],
            "source_failures": s.source_failures,
            "instruments": with_current_gate(s.valuations, s.curves, now, settings.max_curve_age_hours),
            "funds": [fund_view(f, s.navs.get(f.id), now) for f in s.funds],
            "lifecycle": read_lifecycle(lambda s: lifecycle_summary(s.conn, s.db_role, s.bonds)),
            "events": recent_events(s.conn, limit=100),
        }

    @app.get("/api/health")
    def health() -> dict:
        s: State = app.state.s
        now = s.clock()
        s.conn.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "version": VERSION,
            "data_mode": settings.data_mode,
            "heartbeat": now.isoformat(),
            "uptime_seconds": round((now - s.started_at).total_seconds()),
            "last_refresh": s.last_refresh.isoformat() if s.last_refresh else None,
            "curves": {
                ccy: c.status(now, settings.max_curve_age_hours) for ccy, c in s.curves.items()
            },
        }

    @app.get("/api/overview")
    def get_overview() -> dict:
        return overview()

    @app.get("/api/lifecycle")
    def get_lifecycle() -> dict:
        """Read-only. Advancing the simulation date and triggering a default stay in scripts/lifecycle.py."""
        view = read_lifecycle(lambda s: lifecycle_view(s.conn, s.db_role, s.bonds, s.curves, s.valuations))
        return {"database": {"role": app.state.s.db_role, "path": _display_path(settings.db_path)}, **view}

    @app.get("/api/marketmaker")
    def get_marketmaker() -> dict:
        """Read-only. SIMULATED. Every control (pause, resume, kill, reset, limits, runs) stays in scripts/mm.py."""
        s: State = app.state.s
        view = read_lifecycle(lambda s: marketmaker_view(s.conn, s.bonds, s.curves, s.clock(),
                                                         settings.max_curve_age_hours))
        return {"database": {"role": s.db_role, "path": _display_path(settings.db_path)}, **view}

    @app.get("/api/xrpl")
    def get_xrpl() -> dict:
        """Read-only. LIVE MARKET DATA from the RLUSD/XRP monitor's own database, opened read-only.
        The monitor runs separately (scripts/xrpl_monitor.py); nothing here trades."""
        return xrpl_view(settings.xrpl_db_path, app.state.s.clock())

    @app.get("/api/live")
    def get_live() -> dict:
        """Read-only. MAINNET: real funds. The M8 live run after it ended: the final report's figures and the live and
        paper databases of both budget periods, each opened read-only. Nothing here signs, sends or starts a bot."""
        return live_view(settings.live_report_path, settings.xrpl_live_dir, app.state.s.clock())

    @app.get("/api/events")
    def events(kind: str | None = None, limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        return recent_events(app.state.s.conn, limit=limit, kind=kind)

    @app.post("/api/refresh")
    def refresh() -> dict:
        app.state.s.refresh()
        return overview()

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return render_page()

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)  # no icon yet; avoids a 404 in the log

    return app
