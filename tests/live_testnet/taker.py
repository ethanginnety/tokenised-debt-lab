"""TEST ONLY, TESTNET ONLY: take liquidity from the bot's offer, so a real fill can be checked (M8 decision D).

Deliberately not passive, which is why it isn't part of the bot: it lives in tests/, nothing
in debtlab/ or scripts/ may import it (tests/test_xrpl_live_alongside.py scans for that),
and Ethan runs it himself during the testnet run, with a SECOND testnet key:

    python tests/live_testnet/taker.py --key-file PATH --bot-account rBOT --side sell --size 5

--side is the taker's: sell XRP to hit the bot's bid, buy XRP to lift its offer. The offer is
immediate-or-cancel, so nothing rests. Every guard the signer uses applies, and the key is
never printed.

Without --price it reads the bot's offer from the latest validated ledger, skips one that has
already expired, prices one tick through the offer's exact rate, and sends straight away. A
bot offer is often live for only one ledger, so a miss is tried again on a later offer, up to
--attempts. With --price it sends once at that price. Either way it then reads the validated
result and says whether the fill hit the bot's offer, another offer or the AMM. The exit code
is 0 only if the bot's offer was hit.
"""

from __future__ import annotations

import argparse
import sys
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable, NamedTuple

from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import OfferCreate, OfferCreateFlag
from xrpl.transaction import sign
from xrpl.wallet import Wallet

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from debtlab.xrpl_market import load_config, load_testnet_config  # noqa: E402
from debtlab.xrpl_market.live import guards  # noqa: E402
from debtlab.xrpl_market.live.ledger import LedgerOffer, TestnetLedger, exists_on_mainnet  # noqa: E402
from debtlab.xrpl_market.live.signer import Signed, SubmitFailed, Submitter  # noqa: E402
from debtlab.xrpl_market.readonly import XrplReadError  # noqa: E402

MAX_XRP = Decimal(200)
DROPS = Decimal(1_000_000)
FEE_DROPS = "12"
LEDGERS_TO_LAND = 2      # past the last validated ledger; a live offer rarely lasts longer
WAIT_FOR_OFFER_SECONDS = 60
POLL_SECONDS = 0.5


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


def find_target(offers: tuple[LedgerOffer, ...], taker_side: str, close_time: int, size: Decimal) -> LedgerOffer | None:
    """The bot's offer to hit: on the opposite side, not expired by this close time, big enough for the size.
    An offer expired by the latest close can't execute in the next ledger, though the ledger still lists it."""
    want = "buy" if taker_side == "sell" else "sell"
    live = [o for o in offers if o.side == want and o.xrp >= size
            and (o.expiration is None or o.expiration > close_time)]
    return max(live, key=lambda o: o.expiration if o.expiration is not None else float("inf"), default=None)


def limit_through(offer: LedgerOffer, taker_side: str, price_decimals: int) -> Decimal:
    """One tick through the offer's exact rate, from its own amounts. The rate the bot's `status` prints is
    rounded, and can be slightly worse for a taker than the rate on the ledger."""
    tick = Decimal(1).scaleb(-price_decimals)
    rate = offer.rlusd / offer.xrp
    if taker_side == "sell":
        p = rate.quantize(tick, rounding=ROUND_FLOOR)
        return p - tick if p == rate else p
    p = rate.quantize(tick, rounding=ROUND_CEILING)
    return p + tick if p == rate else p


class Outcome(NamedTuple):
    result: str                                            # the validated engine result
    ours: tuple[tuple[int, Decimal, Decimal], ...]         # (bot offer sequence, XRP, RLUSD)
    others: tuple[tuple[str, int, Decimal, Decimal], ...]  # (owner, sequence, XRP, RLUSD)
    amm_xrp: Decimal
    removed: tuple[tuple[str, int], ...]                   # offers deleted with nothing traded: expired or unfunded

    @property
    def hit_bot(self) -> bool:
        return bool(self.ours)


def _amount(a) -> Decimal:
    return Decimal(a) / DROPS if isinstance(a, str) else Decimal(a["value"])


def read_outcome(meta: dict, bot_account: str) -> Outcome:
    """Where the trade went, from a validated transaction's metadata."""
    ours, others, removed = [], [], []
    amm = Decimal(0)
    for node in meta.get("AffectedNodes", []):
        kind, body = next(iter(node.items()))
        fin, prev = body.get("FinalFields", {}), body.get("PreviousFields") or {}
        typ = body.get("LedgerEntryType")
        if typ == "Offer" and kind in ("ModifiedNode", "DeletedNode"):
            if "TakerGets" in prev and "TakerPays" in prev:
                sides = (abs(_amount(prev["TakerGets"]) - _amount(fin["TakerGets"])),
                         abs(_amount(prev["TakerPays"]) - _amount(fin["TakerPays"])))
                xrp, rlusd = sides if isinstance(fin["TakerGets"], str) else sides[::-1]
                if fin.get("Account") == bot_account:
                    ours.append((fin.get("Sequence"), xrp, rlusd))
                else:
                    others.append((fin.get("Account"), fin.get("Sequence"), xrp, rlusd))
            elif kind == "DeletedNode":
                removed.append((fin.get("Account"), fin.get("Sequence")))
        elif typ == "AccountRoot" and "AMMID" in fin and "Balance" in prev:
            amm += abs(Decimal(fin["Balance"]) - Decimal(prev["Balance"])) / DROPS
    return Outcome(meta.get("TransactionResult", "?"), tuple(ours), tuple(others), amm, tuple(removed))


def describe(o: Outcome, bot_account: str) -> list[str]:
    lines = [f"validated result: {o.result}"]
    for seq, xrp, rlusd in o.ours:
        lines.append(f"HIT THE BOT'S OFFER, sequence {seq}: {xrp.normalize():f} XRP for {rlusd.normalize():f} RLUSD")
    for owner, seq, xrp, rlusd in o.others:
        lines.append(f"hit another account's offer, {owner} sequence {seq}: {xrp.normalize():f} XRP for {rlusd.normalize():f} RLUSD")
    if o.amm_xrp:
        lines.append(f"hit the AMM: {o.amm_xrp.normalize():f} XRP")
    for owner, seq in o.removed:
        who = "the bot's" if owner == bot_account else f"{owner}'s"
        lines.append(f"removed {who} offer {seq} without trading: already expired or unfunded")
    if not (o.ours or o.others or o.amm_xrp):
        lines.append("nothing traded")
    return lines


def final_outcome(ledger: TestnetLedger, tx_hash: str, last_ledger: int, bot_account: str,
                  sleep: Callable[[float], None] = time.sleep) -> Outcome | None:
    """The validated result. None once a validated ledger has passed the last one it could land in."""
    while True:
        try:
            r = ledger.client.history("tx", {"transaction": tx_hash}).result
            if r.get("validated") is True:
                return read_outcome(r.get("meta") or r.get("metaData") or {}, bot_account)
        except XrplReadError:
            pass                # not found yet
        try:
            if ledger.client.validated_index() > last_ledger:
                return None
        except XrplReadError:
            pass
        sleep(POLL_SECONDS)


def run(seed: str, cfg: dict, ledger: TestnetLedger, submit: Callable[[Signed], dict], address: str, side: str,
        size: Decimal, bot_account: str, price: Decimal | None, attempts: int,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    """Send until the bot's offer is hit, or the attempts run out. A trade with anything else stops it."""
    decimals = cfg["paper"]["strategy"]["price_decimals"]
    tries = 1 if price is not None else attempts
    tried = 0
    seen = None
    deadline = clock() + WAIT_FOR_OFFER_SECONDS
    while tried < tries:
        h = ledger.latest()
        limit = price
        if price is None:
            target = None
            if h.index != seen:
                seen = h.index
                target = find_target(ledger.account(bot_account, h.index).offers, side, h.close_time, size)
            if target is None:
                if clock() > deadline:
                    print("TESTNET: no live bot offer big enough appeared. Is the bot running and quoting this side?")
                    return 1
                sleep(POLL_SECONDS)
                continue
            limit = limit_through(target, side, decimals)
            print(f"TESTNET: ledger {h.index}: bot offer {target.sequence}, {target.side} {target.xrp} XRP at "
                  f"{target.rlusd / target.xrp:.9f}, expires {target.expiration} (latest close {h.close_time})")
        tried += 1
        last = h.index + LEDGERS_TO_LAND
        signed = build(seed, cfg, side, limit, size, ledger.account(address, h.index).sequence, last, bot_account)
        answer = submit(signed)
        print(f"TESTNET: {side} {size} XRP at {limit} from {address}: first answer {answer.get('engine_result')}, transaction {signed.hash}")
        outcome = final_outcome(ledger, signed.hash, last, bot_account, sleep)
        if outcome is None:
            print("TESTNET: it never reached a validated ledger, so nothing happened")
        else:
            print("\n".join("  " + line for line in describe(outcome, bot_account)))
            if outcome.hit_bot:
                return 0
            if outcome.others or outcome.amm_xrp:
                print("TESTNET: traded, but not with the bot. Stopping rather than spending more of the taker's funds.")
                return 1
        deadline = clock() + WAIT_FOR_OFFER_SECONDS
    print(f"TESTNET: the bot's offer wasn't hit in {tried} attempt(s)")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key-file", type=Path, required=True)
    ap.add_argument("--bot-account", required=True)
    ap.add_argument("--side", choices=("buy", "sell"), required=True)
    ap.add_argument("--price", type=Decimal, help="a fixed price, sent once; without it, read the bot's offer")
    ap.add_argument("--size", type=Decimal, required=True)
    ap.add_argument("--attempts", type=int, default=6)
    args = ap.parse_args(argv)
    cfg = load_testnet_config()
    guards.check_config(cfg)
    path = guards.check_key_path(args.key_file, ROOT)
    seed = path.read_text(encoding="utf-8").strip()
    address = Wallet.from_seed(seed).address
    guards.check_not_on_mainnet(address, lambda a: exists_on_mainnet(a, load_config()["endpoints"]["json_rpc"]))
    ledger = TestnetLedger(cfg)
    sub = Submitter(cfg["endpoints"]["websocket"][0])
    try:
        return run(seed, cfg, ledger, sub.submit, address, args.side, args.size, args.bot_account, args.price, args.attempts)
    except SubmitFailed as e:
        print(f"TESTNET: {e}. Not resent. Check the taker's account on a testnet explorer before trying again.")
        return 2
    finally:
        sub.close()


if __name__ == "__main__":
    sys.exit(main())
