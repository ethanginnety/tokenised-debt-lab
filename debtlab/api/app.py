"""Web app: JSON API plus the dashboard page.

Run with:  python -m debtlab.api   (see README.md)
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from debtlab.audit.db import connect, migrate, record_event, recent_events
from debtlab.config import Settings
from debtlab.instruments import load_instruments
from debtlab.service import curve_view, load_curves, value_all

STATIC = Path(__file__).resolve().parent / "static"
VERSION = "0.1.0-m1"

_SHELL = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
    "</head><body>{body}</body></html>"
)


def render_page(snapshot_json: str | None = None, shell: bool = True) -> str:
    """The dashboard page. With `snapshot_json`, data is embedded (static export)."""
    body = (STATIC / "index.html").read_text()
    if snapshot_json is not None:
        safe = snapshot_json.replace("</", "<\\/")
        body = body.replace("<script>", f"<script>window.__SNAPSHOT__ = {safe};</script>\n<script>", 1)
    return _SHELL.format(body=body) if shell else body


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.conn = connect(settings.db_path)
        migrate(self.conn)
        self.bonds = load_instruments(settings.instruments_file)
        self.curves: dict = {}
        self.valuations: list[dict] = []
        self.last_refresh: datetime | None = None
        self.started_at = datetime.now(timezone.utc)

    def settle_date(self):
        return self.settings.settlement_date or datetime.now(timezone.utc).date()

    def refresh(self) -> None:
        with self.lock:
            now = datetime.now(timezone.utc)
            self.curves = load_curves(self.conn, self.settings, now)
            self.valuations = value_all(
                self.conn, self.bonds, self.curves, self.settle_date(), self.settings, now
            )
            self.last_refresh = now


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = State(settings)
        record_event(state.conn, "app_started",
                     {"version": VERSION, "data_mode": settings.data_mode})
        state.refresh()
        app.state.s = state
        yield
        record_event(state.conn, "app_stopped", {"version": VERSION})
        state.conn.close()

    app = FastAPI(title="Tokenised Debt Lab", version=VERSION, lifespan=lifespan)

    def overview() -> dict:
        s: State = app.state.s
        now = datetime.now(timezone.utc)
        return {
            "generated_at": now.isoformat(),
            "version": VERSION,
            "mode": "simulation",
            "data_mode": settings.data_mode,
            "settle": s.settle_date().isoformat(),
            "last_refresh": s.last_refresh.isoformat() if s.last_refresh else None,
            "curves": [curve_view(c, now, settings.max_curve_age_hours) for c in s.curves.values()],
            "instruments": s.valuations,
            "events": recent_events(s.conn, limit=100),
        }

    @app.get("/api/health")
    def health() -> dict:
        s: State = app.state.s
        now = datetime.now(timezone.utc)
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

    return app
