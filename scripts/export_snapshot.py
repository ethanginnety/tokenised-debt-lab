"""Write a self-contained copy of the dashboard with today's data baked in.

    python scripts/export_snapshot.py [output.html]

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


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "snapshot.html")
    full = "--full" in sys.argv  # include <!doctype>/<head> for opening straight in a browser
    with TestClient(create_app(Settings.from_env())) as client:
        data = client.get("/api/overview").json()
    out.write_text(render_page(json.dumps(data), shell=full))
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
