"""Settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    data_mode: str  # "offline" (illustrative curves) or "online" (official sources)
    max_curve_age_hours: int
    settlement_date: date | None  # None means today (UTC)
    instruments_file: Path
    host: str
    port: int
    holders_file: Path = ROOT / "config" / "holders.json"
    marketmaker_file: Path = ROOT / "config" / "marketmaker.json"
    xrpl_db_path: Path = ROOT / "data" / "xrpl.sqlite3"  # the RLUSD/XRP monitor's own database (M7), read-only here

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("DEBTLAB_DATA_MODE", "offline").strip().lower()
        if mode not in ("offline", "online"):
            raise ValueError("DEBTLAB_DATA_MODE must be 'offline' or 'online'")
        settle = os.getenv("DEBTLAB_SETTLEMENT_DATE", "").strip()
        return cls(
            db_path=Path(os.getenv("DEBTLAB_DB_PATH", str(ROOT / "data" / "debtlab.sqlite3"))),
            data_mode=mode,
            max_curve_age_hours=int(os.getenv("DEBTLAB_MAX_CURVE_AGE_HOURS", "96")),
            settlement_date=date.fromisoformat(settle) if settle else None,
            instruments_file=Path(
                os.getenv("DEBTLAB_INSTRUMENTS", str(ROOT / "config" / "instruments.json"))
            ),
            host=os.getenv("DEBTLAB_HOST", "127.0.0.1"),
            port=int(os.getenv("DEBTLAB_PORT", "8000")),
            holders_file=Path(os.getenv("DEBTLAB_HOLDERS", str(ROOT / "config" / "holders.json"))),
            marketmaker_file=Path(os.getenv("DEBTLAB_MARKETMAKER", str(ROOT / "config" / "marketmaker.json"))),
            xrpl_db_path=Path(os.getenv("DEBTLAB_XRPL_DB", str(ROOT / "data" / "xrpl.sqlite3"))),
        )
