"""Save today's raw downloads from the official sources as test fixtures.

    python scripts/save_fixtures.py

Writes, unchanged, exactly what each source returned:
    tests/fixtures/real/treasury_YYYY-MM-DD.csv
    tests/fixtures/real/ecb_YYYY-MM-DD.json   ({tenor: csv text})

The tests in tests/test_real_fixtures.py then parse every saved file, so a
change in a source's format shows up as a failing test, not a wrong price.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.marketdata.adapters import (  # noqa: E402
    ECB_TENORS,
    ECB_URL,
    TREASURY_URL,
    MarketDataError,
    _get,
    parse_ecb_csv,
    parse_treasury_csv,
    with_retries,
)

OUT = ROOT / "tests" / "fixtures" / "real"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.date().isoformat()
    ok = True

    try:
        text = with_retries(lambda: _get(TREASURY_URL.format(year=now.year)))
        snap = parse_treasury_csv(text, now)  # check it parses before saving
        path = OUT / f"treasury_{stamp}.csv"
        path.write_text(text, encoding="utf-8", newline="")
        print(f"saved {path.relative_to(ROOT)}  latest row {snap.observation_date}, "
              f"{len(snap.curve.points)} maturities")
    except MarketDataError as exc:
        ok = False
        print(f"Treasury FAILED: {exc}")

    try:
        texts = {t: with_retries(lambda t=t: _get(ECB_URL.format(tenor=t))) for t in ECB_TENORS}
        snap = parse_ecb_csv(texts, now)
        path = OUT / f"ecb_{stamp}.json"
        path.write_text(json.dumps(texts, indent=1), encoding="utf-8")
        print(f"saved {path.relative_to(ROOT)}  observation {snap.observation_date}, "
              f"{len(snap.curve.points)} maturities")
    except MarketDataError as exc:
        ok = False
        print(f"ECB FAILED: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
