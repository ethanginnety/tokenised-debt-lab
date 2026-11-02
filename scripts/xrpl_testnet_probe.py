"""M8 step 1: read the RLUSD/XRP facts from the XRP Ledger TESTNET and save them. READ-ONLY.

    python scripts/xrpl_testnet_probe.py                    # saves to tests/fixtures/real/xrpl_testnet/<UTC date>/
    python scripts/xrpl_testnet_probe.py --monitor-db PATH  # also: expiry timing from the monitor's recording

Public read methods only, through the same client as M7. No key, no wallet, nothing signed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import closetimes, load_config, verify  # noqa: E402
from debtlab.xrpl_market.readonly import ReadOnlyClient  # noqa: E402
from debtlab.xrpl_market.view import open_readonly  # noqa: E402

CONFIG = ROOT / "config" / "xrpl_testnet.json"
LEDGER_SAMPLE = 60


def probe(cfg: dict, out: Path) -> dict:
    url, r = cfg["endpoints"]["json_rpc"][0], cfg["rlusd"]
    rlusd, xrp = {"currency": r["currency_hex"], "issuer": r["issuer"]}, {"currency": "XRP"}
    out.mkdir(parents=True, exist_ok=True)
    with ReadOnlyClient(url) as c:
        (out / "server_info.json").write_bytes(c.server("server_info").raw)
        state = c.server("server_state")
        (out / "server_state.json").write_bytes(state.raw)
        seq = int(state.result["state"]["validated_ledger"]["seq"])
        reads = {"issuer_account_info": ("account_info", {"account": r["issuer"]}),
                 "issuer_gateway_balances": ("gateway_balances", {"account": r["issuer"]}),
                 "book_bids": ("book_offers", {"taker_gets": rlusd, "taker_pays": xrp, "limit": 100}),
                 "book_asks": ("book_offers", {"taker_gets": xrp, "taker_pays": rlusd, "limit": 100}),
                 "amm_info": ("amm_info", {"asset": xrp, "asset2": rlusd})}
        for name, (method, params) in reads.items():
            (out / f"{name}.json").write_bytes(c.at(method, params, seq).raw)
        headers = []
        for i in range(LEDGER_SAMPLE):
            led = c.at("ledger", {}, seq - i).result["ledger"]
            headers.append({k: led.get(k) for k in ("ledger_index", "close_time", "close_time_resolution", "close_flags")})
    (out / "ledger_headers.json").write_text(json.dumps(headers, indent=1) + "\n", encoding="utf-8", newline="")
    manifest = {"endpoint": url, "ledger_index": seq, "issuer": r["issuer"], "currency_hex": r["currency_hex"],
                "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="")
    return manifest


def report(out: Path) -> None:
    res = lambda name: json.loads((out / f"{name}.json").read_text(encoding="utf-8"))["result"]  # noqa: E731
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    print(f"TESTNET ledger {m['ledger_index']}, {m['endpoint']}, fetched {m['fetched_utc']}")
    print(f"network_id {res('server_info')['info'].get('network_id')}")
    s = verify.issuer_settings(res("issuer_account_info"))
    print(f"Issuer {s.address}: Domain {s.domain}; transfer fee {s.transfer_fee_pct}%; tick size {s.tick_size}")
    print("  flags: " + ", ".join(f"{k}={v}" for k, v in sorted(s.flags.items())))
    print(f"  outstanding: {verify.obligations(res('issuer_gateway_balances'), m['currency_hex']):,.2f}")
    n = verify.network_costs(res("server_state"))
    print(f"Reserves {n.reserve_base_xrp} + {n.reserve_inc_xrp} XRP; fee {n.base_fee_drops} drops")
    print(f"Book: {len(res('book_bids').get('offers', []))} bids, {len(res('book_asks').get('offers', []))} asks")
    amm = res("amm_info")["amm"]
    print(f"AMM: {int(amm['amount']) / 1e6:,.2f} XRP, {float(amm['amount2']['value']):,.2f} RLUSD")
    h = json.loads((out / "ledger_headers.json").read_text(encoding="utf-8"))
    print("Close-time resolution seen:", sorted({x["close_time_resolution"] for x in h}),
          "close_flags seen:", sorted({x["close_flags"] for x in h}))


def timing(path: Path) -> None:
    conn = open_readonly(path)
    p = lambda s: datetime.fromisoformat(s).timestamp()  # noqa: E731
    rows = [(r[0], p(r[1]), p(r[2])) for r in conn.execute(
        "SELECT ledger_index, close_time, received_at FROM xrpl_snapshots ORDER BY ledger_index")]
    off = closetimes.boundary_offsets(rows)
    print(f"Monitor: {len(rows)} ledgers, {len(off)} boundaries. Last ledger able to execute, seen after E: "
          f"median {statistics.median(off):.2f} s, max {max(off):.2f} s; "
          f"close time ahead of receipt: at most {max(r[1] - r[2] for r in rows):.2f} s")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--monitor-db", type=Path)
    args = ap.parse_args(argv)
    cfg = load_config(CONFIG)
    day = datetime.now(timezone.utc).date().isoformat()
    out = args.out or ROOT / "tests" / "fixtures" / "real" / "xrpl_testnet" / day
    probe(cfg, out)
    report(out)
    if args.monitor_db:
        timing(args.monitor_db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
