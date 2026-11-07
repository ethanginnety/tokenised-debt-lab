"""M7 step 7: the end-of-run paper report (brief §7.5). PAPER: read-only on both databases.

    python scripts/xrpl_paper_report.py --db C:\\debtlab-data\\xrpl_paper.sqlite3 --monitor-db C:\\debtlab-data\\xrpl.sqlite3 \\
        [--hours 24] [--out results/m7_paper_report]

Writes OUT.md (to read) and OUT.json (exact figures). The result covers the first --hours of
uptime (decision F); what came after is reported separately.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config  # noqa: E402
from debtlab.xrpl_market.paper import runreport  # noqa: E402
from debtlab.xrpl_market.view import open_readonly  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--monitor-db", type=Path, required=True)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "m7_paper_report")
    a = ap.parse_args(argv)
    r = runreport.build(open_readonly(a.db), open_readonly(a.monitor_db), load_config(), a.hours)
    a.out.with_suffix(".json").write_text(runreport.to_json(r) + "\n", encoding="utf-8")
    a.out.with_suffix(".md").write_text(runreport.render(r) + "\n", encoding="utf-8")
    print(f"PAPER: wrote {a.out.with_suffix('.md')} and .json; result up to {r['cutoff']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
