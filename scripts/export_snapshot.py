"""Write a self-contained copy of the dashboard with today's data baked in.

    python scripts/export_snapshot.py [output.html] [--full]

--full adds <!doctype>/<head>, for opening the file straight in a browser.

Useful for sharing a point-in-time view without running the server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debtlab.api.app import create_app, render_page  # noqa: E402
from debtlab.config import Settings  # noqa: E402


def parse_args(argv: list[str]) -> tuple[Path, bool]:
    """The output path is the first non-flag argument, wherever --full appears."""
    positional = [a for a in argv if not a.startswith("--")]
    return Path(positional[0] if positional else "snapshot.html"), "--full" in argv


def main() -> None:
    out, full = parse_args(sys.argv[1:])
    with TestClient(create_app(Settings.from_env())) as client:
        data = client.get("/api/overview").json()
    out.write_text(render_page(json.dumps(data), shell=full), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
