"""TEST ONLY, TESTNET ONLY: take liquidity from the bot's offer, so a real fill can be checked (M8 decision D).

Deliberately not passive, which is why it isn't part of the bot: it lives in tests/, nothing
in debtlab/ or scripts/ may import it (tests/test_xrpl_live_alongside.py scans for that),
and Ethan runs it himself during the testnet run, with a SECOND testnet key:

    python tests/live_testnet/taker.py --key-file PATH --bot-account rBOT --side sell --price 0.0799 --size 10

--side is the taker's: sell XRP to hit the bot's bid, buy XRP to lift its offer. The offer is
immediate-or-cancel, so nothing rests. Every guard the signer uses applies, and the key is
never printed.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import OfferCreate, OfferCreateFlag
from xrpl.transaction import sign
from xrpl.wallet import Wallet

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config, load_testnet_config  # noqa: E402
from debtlab.xrpl_market.live import guards  # noqa: E402
from debtlab.xrpl_market.live.ledger import TestnetLedger, exists_on_mainnet  # noqa: E402
from debtlab.xrpl_market.live.signer import Signed, Submitter  # noqa: E402

MAX_XRP = Decimal(200)
DROPS = Decimal(1_000_000)
FEE_DROPS = "12"


class Refused(Exception):
    pass


def build(seed: str, cfg: dict, side: str, price: Decimal, size: Decimal, sequence: int, last_ledger: int, bot_account: str) -> Signed:
    guards.check_config(cfg)
    if size <= 0 or size > MAX_XRP:
        raise Refused(f"size {size} XRP: between 0 and {MAX_XRP} only")
    if side not in ("buy", "sell"):
        raise Refused(f"unknown side {side}")
    wallet = Wallet.from_seed(seed)
    if wallet.address == bot_account:
        raise Refused("this is the bot's own key: use a second testnet key")
    token = IssuedCurrencyAmount(currency=cfg["rlusd"]["currency_hex"], issuer=cfg["rlusd"]["issuer"],
                                 value=format((size * price).normalize(), "f"))
    xrp = str(int(size * DROPS))
    gets, pays = (token, xrp) if side == "buy" else (xrp, token)
    tx = OfferCreate(account=wallet.address, sequence=sequence, fee=FEE_DROPS, last_ledger_sequence=last_ledger,
                     flags=OfferCreateFlag.TF_IMMEDIATE_OR_CANCEL, taker_gets=gets, taker_pays=pays)
    s = sign(tx, wallet)
    return Signed(s.blob(), s.get_hash(), "OfferCreate", sequence, last_ledger)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key-file", type=Path, required=True)
    ap.add_argument("--bot-account", required=True)
    ap.add_argument("--side", choices=("buy", "sell"), required=True)
    ap.add_argument("--price", type=Decimal, required=True)
    ap.add_argument("--size", type=Decimal, required=True)
    args = ap.parse_args(argv)
    cfg = load_testnet_config()
    guards.check_config(cfg)
    path = guards.check_key_path(args.key_file, ROOT)
    seed = path.read_text(encoding="utf-8").strip()
    address = Wallet.from_seed(seed).address
    guards.check_not_on_mainnet(address, lambda a: exists_on_mainnet(a, load_config()["endpoints"]["json_rpc"]))
    ledger = TestnetLedger(cfg)
    h = ledger.latest()
    st = ledger.account(address, h.index)
    signed = build(seed, cfg, args.side, args.price, args.size, st.sequence, h.index + 4, args.bot_account)
    sub = Submitter(cfg["endpoints"]["websocket"][0])
    try:
        r = sub.submit(signed)
    finally:
        sub.close()
    print(f"TESTNET: {args.side} {args.size} XRP at {args.price} from {address}: {r.get('engine_result')}, transaction {signed.hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
