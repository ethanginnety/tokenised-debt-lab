"""M7 step 1: read RLUSD/XRP from the XRP Ledger mainnet, save it, and say go or no-go.

    python scripts/xrpl_probe.py                    # saves to tests/fixtures/real/xrpl/<UTC date>/
    python scripts/xrpl_probe.py --out DIR --check-account rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK

READ-ONLY: public read methods only (debtlab.xrpl_market.readonly). No keys, no wallet.

Everything ledger-related is read at ONE validated ledger index: the issuer, its
outstanding RLUSD, both sides of the order book (every page) and the AMM pool. The
domain's xrp-ledger.toml is fetched too, since an account's Domain field is only a claim
until the domain lists the account. Raw responses are saved byte for byte, with a
manifest, so tests/test_xrpl_verify.py can check them offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config  # noqa: E402
from debtlab.xrpl_market.readonly import ReadOnlyClient, XrplReadError  # noqa: E402
from debtlab.xrpl_market import verify  # noqa: E402

MAX_BOOK_PAGES = 10
BOOK_PAGE_LIMIT = 400


def probe(url: str, cfg: dict, out: Path, check_accounts: list[str]) -> dict:
    r = cfg["rlusd"]
    rlusd = {"currency": r["currency_hex"], "issuer": r["issuer"]}
    xrp = {"currency": "XRP"}
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def save(name: str, raw: bytes) -> None:
        (out / name).write_bytes(raw)
        files[name] = name

    with ReadOnlyClient(url) as c:
        state = c.server("server_state")
        save("server_state.json", state.raw)
        seq = int(state.result["state"]["validated_ledger"]["seq"])
        save("issuer_account_info.json", c.at("account_info", {"account": r["issuer"]}, seq).raw)
        save("issuer_gateway_balances.json", c.at("gateway_balances", {"account": r["issuer"]}, seq).raw)
        for side, gets, pays in (("bids", rlusd, xrp), ("asks", xrp, rlusd)):
            marker = None
            for page in range(MAX_BOOK_PAGES):
                params = {"taker_gets": gets, "taker_pays": pays, "limit": BOOK_PAGE_LIMIT}
                if marker:
                    params["marker"] = marker
                resp = c.at("book_offers", params, seq)
                save(f"book_{side}_{page}.json", resp.raw)
                marker = resp.result.get("marker")
                if not marker:
                    break
        save("amm_info.json", c.at("amm_info", {"asset": xrp, "asset2": rlusd}, seq).raw)
        for acct in check_accounts:
            save(f"account_info_{acct}.json", c.at("account_info", {"account": acct}, seq).raw)

    toml = httpx.get(r["toml_url"], timeout=20, follow_redirects=True)
    toml.raise_for_status()
    save("ripple_xrp-ledger.toml", toml.content)

    manifest = {"endpoint": url, "ledger_index": seq,
                "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "toml_url": r["toml_url"], "issuer": r["issuer"], "currency_hex": r["currency_hex"],
                "checked_accounts": check_accounts, "files": sorted(files)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="")
    return manifest


def report(out: Path) -> verify.GoNoGo:
    """Print what the saved files say. Works offline on any saved probe."""
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    res = lambda name: json.loads((out / name).read_text(encoding="utf-8"))["result"]  # noqa: E731
    cur = m["currency_hex"]
    toml = (out / "ripple_xrp-ledger.toml").read_text(encoding="utf-8")

    print(f"Ledger {m['ledger_index']} (validated), {m['endpoint']}, fetched {m['fetched_utc']}")
    s = verify.issuer_settings(res("issuer_account_info.json"))
    listed = verify.listed_in_toml(toml, s.address, "RLUSD")
    print(f"Issuer {s.address}: Domain {s.domain}; listed as RLUSD issuer in the domain's toml: {listed}")
    print(f"  transfer fee {s.transfer_fee_pct}%, tick size {s.tick_size or 'none (15 digits)'}")
    print("  flags: " + ", ".join(f"{k}={v}" for k, v in sorted(s.flags.items())))
    print(f"  RLUSD outstanding: {verify.obligations(res('issuer_gateway_balances.json'), cur):,.2f}")
    for acct in m["checked_accounts"]:
        o = verify.issuer_settings(res(f"account_info_{acct}.json"))
        print(f"Checked {acct}: Domain {o.domain}; listed in toml: {verify.listed_in_toml(toml, acct, 'RLUSD')}")

    n = verify.network_costs(res("server_state.json"))
    print(f"Reserves: base {n.reserve_base_xrp} XRP, {n.reserve_inc_xrp} XRP per object; "
          f"fee {n.base_fee_drops} drops (now {n.current_fee_drops})")

    def side(name: str) -> verify.Side:
        pages = sorted(out.glob(f"book_{name}_*.json"))
        merged = {"offers": [], "ledger_index": m["ledger_index"]}
        for p in pages:
            r = json.loads(p.read_text(encoding="utf-8"))["result"]
            merged["offers"] += r.get("offers", [])
            if p == pages[-1]:
                merged["last_page_size"] = len(r.get("offers", []))
                if "marker" in r:
                    merged["marker"] = r["marker"]
        return verify.book_side(merged, cur, page_limit=BOOK_PAGE_LIMIT)

    bids, asks = side("bids"), side("asks")
    for b in (bids, asks):
        print(f"Book {b.side}s: {b.funded_offers}/{b.offers} funded offers, {b.xrp:,.2f} XRP / "
              f"{b.rlusd:,.2f} RLUSD, best {b.best:.6f}{'' if b.complete else ' (a full page: there may be more offers further from the mid)'}")
    if bids.best and asks.best:
        mid = (bids.best + asks.best) / 2
        print(f"Spread {(asks.best - bids.best) / mid * 10000:.2f} bp around mid {mid:.6f} RLUSD per XRP")
    a = verify.amm(res("amm_info.json"), cur)
    print(f"AMM {a.account}: {a.xrp:,.2f} XRP + {a.rlusd:,.2f} RLUSD, price {a.price:.6f}, fee {a.trading_fee_pct}%")

    ledgers = {s.ledger_index, bids.ledger_index, asks.ledger_index, a.ledger_index, m["ledger_index"]}
    g = verify.go_no_go(bids, asks, same_ledger=len(ledgers) == 1)
    if not listed:
        g = verify.GoNoGo(False, g.reasons + ["issuer not confirmed by its domain's toml"])
    print("GO: the order book has funded offers on both sides" if g.go else "NO-GO: " + "; ".join(g.reasons))
    return g


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, help="folder to save into (default: tests/fixtures/real/xrpl/<UTC date>)")
    ap.add_argument("--check-account", action="append", default=None,
                    help="another account to read and check against the toml (default: the known look-alikes)")
    ap.add_argument("--report-only", type=Path, help="don't connect; report on a saved probe folder")
    args = ap.parse_args()
    if args.report_only:
        return 0 if report(args.report_only).go else 1
    out = args.out or ROOT / "tests" / "fixtures" / "real" / "xrpl" / datetime.now(timezone.utc).date().isoformat()
    accounts = args.check_account if args.check_account is not None else list(cfg["known_lookalikes"])
    last: Exception | None = None
    for url in cfg["endpoints"]["json_rpc"]:
        try:
            probe(url, cfg, out, accounts)
            break
        except (XrplReadError, httpx.HTTPError) as e:
            print(f"{url} failed: {e}; trying the next endpoint", file=sys.stderr)
            last = e
    else:
        print(f"Every endpoint failed; last error: {last}", file=sys.stderr)
        return 2
    print(f"Saved to {out}\n")
    return 0 if report(out).go else 1


if __name__ == "__main__":
    sys.exit(main())
