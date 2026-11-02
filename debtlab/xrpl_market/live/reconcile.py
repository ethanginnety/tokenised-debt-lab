"""What a validated transaction did to the account, and whether the books match the ledger (M8).

Pure functions. A transaction's metadata lists the ledger entries it changed, with their
fields before and after. From those:

    our XRP and RLUSD balance changes, and the fee if we sent it
    maker fills: one of our offers consumed by someone else's transaction
    taker fill: our own OfferCreate moved funds as it arrived, so it took liquidity
    external: anything else that moved our balances (a faucet or a wallet payment)
    which of our offers it created or removed

Everything left after the fills and the fee counts as external, so the parts always add up
to the balance change, and an unexplained change can't hide. The one exception is rounding.
RLUSD amounts hold 16 significant digits, so a balance can move by a hair more or less than
the offer did: 1E-13 and 5E-15 RLUSD on the saved real fills, with XRP exact. On a fill, a
difference up to `ROUNDING_RLUSD` is booked as rounding. Anything larger is still external.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from debtlab.money import D, ZERO
from debtlab.xrpl_market.live.ledger import AccountState
from debtlab.xrpl_market.trades import _tx_parts, _xrp_or_token

DROPS = Decimal(1_000_000)
ROUNDING_RLUSD = Decimal("1E-9")


@dataclass(frozen=True)
class MakerFill:
    sequence: int
    side: str           # ours
    xrp: Decimal
    rlusd: Decimal


@dataclass(frozen=True)
class Effect:
    hash: str
    ledger_index: int
    tx_type: str
    result: str
    ours: bool                  # we sent it
    sequence: int | None        # its Sequence, if ours
    fee_xrp: Decimal
    xrp: Decimal                # the whole change in our XRP, fee included
    rlusd: Decimal
    maker_fills: tuple[MakerFill, ...] = ()
    taker_xrp: Decimal = ZERO   # our OfferCreate on arrival: signed, + we received
    taker_rlusd: Decimal = ZERO
    external_xrp: Decimal = ZERO
    external_rlusd: Decimal = ZERO
    rounding_rlusd: Decimal = ZERO
    created: int | None = None              # our offer left resting by this transaction
    removed: tuple[int, ...] = field(default=())   # our offers it removed, whatever the reason

    @property
    def took(self) -> bool:
        return self.taker_xrp != 0 or self.taker_rlusd != 0

    @property
    def external(self) -> bool:
        return self.external_xrp != 0 or self.external_rlusd != 0


def _line_balance(fields: dict, account: str, rlusd_hex: str, issuer: str) -> Decimal | None:
    bal = fields.get("Balance")
    low, high = fields.get("LowLimit", {}).get("issuer"), fields.get("HighLimit", {}).get("issuer")
    if not isinstance(bal, dict) or bal.get("currency") != rlusd_hex or {low, high} != {account, issuer}:
        return None
    # A trust line's balance is from the low account's side.
    return D(bal["value"]) if low == account else -D(bal["value"])


def effect(msg: dict, account: str, rlusd_hex: str, issuer: str) -> Effect:
    tx, meta, h, li = _tx_parts(msg)
    ours = tx.get("Account") == account
    result = meta.get("TransactionResult", "")
    dx = dr = ZERO
    fills: list[MakerFill] = []
    created = None
    removed: list[int] = []

    for wrapper in meta.get("AffectedNodes", []):
        (kind, node), = wrapper.items()
        typ = node.get("LedgerEntryType")
        fin = node.get("FinalFields") or node.get("NewFields") or {}
        prev = node.get("PreviousFields") or {}
        if typ == "AccountRoot" and fin.get("Account") == account:
            if kind == "ModifiedNode" and "Balance" in prev:
                dx += (D(fin["Balance"]) - D(prev["Balance"])) / DROPS
            elif kind == "CreatedNode":
                dx += D(fin.get("Balance", 0)) / DROPS
        elif typ == "RippleState":
            after = _line_balance(fin, account, rlusd_hex, issuer)
            if after is None:
                continue
            if kind == "CreatedNode":
                dr += after
            elif "Balance" in prev:
                dr += after - _line_balance({**fin, "Balance": prev["Balance"]}, account, rlusd_hex, issuer)
        elif typ == "Offer" and fin.get("Account") == account:
            seq = int(fin["Sequence"])
            if kind == "CreatedNode":
                created = seq
                continue
            if kind == "DeletedNode":
                removed.append(seq)
            if "TakerGets" in prev and "TakerPays" in prev:
                g0, g1 = _xrp_or_token(prev["TakerGets"], rlusd_hex, issuer), _xrp_or_token(fin["TakerGets"], rlusd_hex, issuer)
                p0, p1 = _xrp_or_token(prev["TakerPays"], rlusd_hex, issuer), _xrp_or_token(fin["TakerPays"], rlusd_hex, issuer)
                if None in (g0, g1, p0, p1):
                    continue
                gave, got = g0[1] - g1[1], p0[1] - p1[1]
                if gave <= 0 and got <= 0:
                    continue
                if g0[0] == "RLUSD":      # the offer gave RLUSD for XRP: our bid
                    fills.append(MakerFill(seq, "buy", got, gave))
                else:
                    fills.append(MakerFill(seq, "sell", gave, got))

    fee = D(tx.get("Fee", 0)) / DROPS if ours else ZERO
    filled_x = sum((f.xrp if f.side == "buy" else -f.xrp for f in fills), ZERO)
    filled_r = sum((-f.rlusd if f.side == "buy" else f.rlusd for f in fills), ZERO)
    rest_x, rest_r = dx + fee - filled_x, dr - filled_r
    trading = ours and tx.get("TransactionType") == "OfferCreate" and result == "tesSUCCESS"
    rounding = ZERO
    if (fills or trading) and rest_x == 0 and rest_r != 0 and abs(rest_r) <= ROUNDING_RLUSD:
        rounding, rest_r = rest_r, ZERO
    return Effect(h, li, tx.get("TransactionType", ""), result, ours, int(tx["Sequence"]) if ours and "Sequence" in tx else None,
                  fee, dx, dr, tuple(fills),
                  rest_x if trading else ZERO, rest_r if trading else ZERO,
                  ZERO if trading else rest_x, ZERO if trading else rest_r, rounding,
                  created, tuple(removed))


def problems(state: AccountState, expected_xrp: Decimal, expected_rlusd: Decimal, open_sequences: set[int],
             close_time: int) -> list[str]:
    """Every way the ledger disagrees with the bot's books. Any one of them stops the bot.

    An offer past its Expiration can still sit in the ledger until something removes it, but
    it can't execute, so it isn't one the bot needs to know about."""
    out = []
    if state.xrp != expected_xrp:
        out.append(f"XRP on the ledger {state.xrp}, in the books {expected_xrp}")
    if state.rlusd != expected_rlusd:
        out.append(f"RLUSD on the ledger {state.rlusd}, in the books {expected_rlusd}")
    for o in state.offers:
        alive = o.expiration is None or o.expiration > close_time
        if alive and o.sequence not in open_sequences:
            out.append(f"offer {o.sequence} is on the ledger but not in the books")
    return out
