"""Run the simulated market maker over many seeds and compare. SIMULATED: not evidence of real profitability.

    python scripts/mm_seeds.py [--seeds 200] [--informed 0.045] [--profiles normal,stress] [--out results/x.json]

Each run is one simulated day on its own in-memory copy of the main database (read
only, never changed), set up as the app would set it up (migrations, the market
maker's book and limits), with the bot running, on the latest official curves. Every
run is checked: the P&L split must equal the total from cash and inventory, and the
ledger must pass its integrity check. Then, for each profile, with and without informed
clients (who arrive on top of the uninformed ones, so the gap between the
columns is adverse selection, not lost volume), it prints the mean and standard deviation over seeds of each P&L line, the
fills and the markouts, and how often the daily-loss limit paused the bot.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debtlab import ledger  # noqa: E402
from debtlab.audit.db import latest_curve, migrate  # noqa: E402
from debtlab.config import Settings  # noqa: E402
from debtlab.instruments import load_instruments  # noqa: E402
from debtlab.marketdata.models import MARKET_KINDS  # noqa: E402
from debtlab.marketmaker import controls, simulation  # noqa: E402
from debtlab.marketmaker.book import load_book_config, seed_book  # noqa: E402
from debtlab.money import D  # noqa: E402

_W: dict = {}  # per worker process: the template database and what every run needs


def _memory() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _setup(db_path: str, instruments_file: str, marketmaker_file: str) -> None:
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    template = _memory()
    src.backup(template)
    src.close()
    migrate(template)
    instruments = load_instruments(Path(instruments_file))
    seed_book(template, load_book_config(Path(marketmaker_file)), instruments)
    controls.seed_limits(template, Path(marketmaker_file))
    curves = {c: s for c in ("GBP", "USD") if (s := latest_curve(template, c, MARKET_KINDS))}
    start = max(s.fetched_at for s in curves.values())
    controls.resume(template, "mm_seeds", "many-seed comparison (in memory)", start)
    _W.update(template=template, instruments=instruments, curves=curves, start=start,
              config=Path(marketmaker_file))


def _one(job: tuple[str, str, int]) -> tuple[str, str, dict]:
    profile, informed, seed = job
    conn = _memory()
    _W["template"].backup(conn)
    params = simulation.load_sim_params(_W["config"], profile, informed_arrival_prob=informed)
    s = simulation.run(conn, f"{profile}-{informed}-{seed}", str(seed), _W["instruments"], _W["curves"],
                       _W["start"].date(), _W["start"], 96, _W["config"], params)
    for ccy, p in s["pnl"].items():
        if D(p["total"]) != D(p["total_from_ledger"]):
            raise AssertionError(f"seed {seed} {ccy}: split {p['total']} != ledger {p['total_from_ledger']}")
    problems = ledger.check_integrity(conn)
    if problems:
        raise AssertionError(f"seed {seed}: ledger integrity {problems[:2]}")
    conn.close()
    return profile, informed, simulation.flatten(s)


def _curves(settings: Settings) -> dict:
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {c: s for c in ("GBP", "USD") if (s := latest_curve(conn, c, MARKET_KINDS))}
    finally:
        conn.close()


def _fmt(mean: float, sd: float, count: bool) -> str:
    return f"{mean:.2f} ± {sd:.2f}" if count else f"{mean:,.0f} ± {sd:,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--informed", default="0.045",
                    help="informed arrival chance per bond per step for the 'with informed' runs (on top)")
    ap.add_argument("--profiles", default="normal,stress")
    ap.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) - 2, 1))
    ap.add_argument("--out", type=Path, help="write every run's numbers here as JSON")
    args = ap.parse_args()
    settings = Settings.from_env()
    profiles = args.profiles.split(",")
    flows = ["0", args.informed]
    jobs = [(p, f, s) for p in profiles for f in flows for s in range(1, args.seeds + 1)]

    results: dict[tuple[str, str], list[dict]] = {(p, f): [] for p in profiles for f in flows}
    with ProcessPoolExecutor(args.workers, initializer=_setup,
                             initargs=(str(settings.db_path), str(settings.instruments_file),
                                       str(settings.marketmaker_file))) as pool:
        for i, (p, f, flat) in enumerate(pool.map(_one, jobs, chunksize=4), 1):
            results[(p, f)].append(flat)
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} runs", file=sys.stderr, flush=True)

    print(f"{simulation.LABEL}. {args.seeds} seeds per column, one simulated day each, latest official curves.")
    for p in profiles:
        params = simulation.load_sim_params(settings.marketmaker_file, p)
        print(f"  {p}: jump probability {params.jump_prob} a step, implied daily yield s.d. "
              f"{simulation.daily_sd_bp(params):.1f} bp")
    print("Every run: P&L split = total from cash and inventory exactly; ledger integrity ok.\n")
    cols = [(p, f) for p in profiles for f in flows]
    stats = {c: simulation.mean_sd(results[c]) for c in cols}
    names = {"0": "uninformed only", args.informed: f"+ informed {float(args.informed):.1%}/step"}
    header = "| mean ± s.d. | " + " | ".join(f"{p}, {names[f]}" for p, f in cols) + " |"
    print(header)
    print("|" + "---|" * (len(cols) + 1))
    for key in stats[cols[0]]:
        count = "fills" in key or "tripped" in key
        print(f"| {key} | " + " | ".join(_fmt(*stats[c].get(key, (0.0, 0.0)), count) for c in cols) + " |")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({
            "label": simulation.LABEL, "seeds": args.seeds, "informed_arrival_prob": args.informed,
            "curves": {c: s.id for c, s in _curves(settings).items()},
            "runs": {f"{p}|{f}": rows for (p, f), rows in results.items()},
        }, indent=1), encoding="utf-8")
        print(f"\nevery run's numbers: {args.out}")


if __name__ == "__main__":
    main()
