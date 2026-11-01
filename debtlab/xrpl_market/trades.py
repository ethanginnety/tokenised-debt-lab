"""Trades on RLUSD/XRP, derived from a validated transaction's metadata.

The XRP Ledger has no trade tape. What a transaction did is in its metadata: the ledger
entries it changed, with their fields before (`PreviousFields`) and after (`FinalFields`).

- **Book trade:** an Offer on this pair whose amounts went down. The consumed amounts are
  the trade; the offer's owner was the maker. A deleted offer whose amounts didn't change
  (cancelled, expired, found unfunded) is not a trade.
- **AMM swap:** the pool's XRP (the AMM account's balance) and RLUSD (its trust line to
  the issuer) moved in opposite directions. Deposits and withdrawals move both the same
  way and are left out by transaction type as well.

Only successful (`tesSUCCESS`), validated transactions count. Prices are RLUSD per XRP.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from debtlab.money import D, ZERO
from debtlab.xrpl_market.verify import DROPS_PER_XRP

NOT_SWAPS = frozenset({"AMMCreate", "AMMDeposit", "AMMWithdraw", "AMMVote", "AMMBid", "AMMDelete", "AMMClawback"})


@dataclass(frozen=True)
class Trade:
    tx_hash: str
    node: str                 # consumed offer's ledger index, or "amm"
    ledger_index: int
    venue: str                # "book" or "amm"
    taker_side: str           # "buy_xrp" or "sell_xrp"
    price: Decimal
    xrp: Decimal
    rlusd: Decimal
    maker: str | None
    tx_type: str
    pool_price_before: Decimal | None = None
    pool_price_after: Decimal | None = None
    pool_xrp_before: Decimal | None = None
    pool_rlusd_before: Decimal | None = None


class NotValidated(ValueError):
    pass


def _tx_parts(msg: dict) -> tuple[dict, dict, str, int]:
    """(tx, meta, hash, ledger_index) from a stream message or a `ledger`/`tx` result,
    in either API version's layout."""
    tx = msg.get("tx_json") or msg.get("transaction") or msg.get("tx") or msg
    meta = msg.get("meta") or msg.get("metaData")
    h = msg.get("hash") or tx.get("hash")
    li = msg.get("ledger_index") or tx.get("ledger_index")
    if not isinstance(meta, dict) or not h or li is None:
        raise ValueError("not a transaction with metadata")
    return tx, meta, h, int(li)


def _xrp_or_token(a, rlusd_hex: str, issuer: str) -> tuple[str, Decimal] | None:
    """("XRP"|"RLUSD", value) for this pair's two assets, None for anything else."""
    if isinstance(a, str):
        return "XRP", D(a) / DROPS_PER_XRP
    if a.get("currency") == rlusd_hex and a.get("issuer") == issuer:
        return "RLUSD", D(a["value"])
    return None


def extract(msg: dict, rlusd_hex: str, issuer: str, amm_account: str | None) -> list[Trade]:
    if msg.get("validated") is not True:
        raise NotValidated("only validated transactions count")
    tx, meta, h, li = _tx_parts(msg)
    if meta.get("TransactionResult") != "tesSUCCESS":
        return []
    ttype = tx.get("TransactionType", "")
    out: list[Trade] = []
    amm_xrp = amm_rl = None   # (before, after)

    for wrapper in meta.get("AffectedNodes", []):
        (kind, node), = wrapper.items()
        if kind not in ("ModifiedNode", "DeletedNode"):
            continue
        typ = node.get("LedgerEntryType")
        prev, fin = node.get("PreviousFields") or {}, node.get("FinalFields") or {}

        if typ == "Offer" and "TakerGets" in prev and "TakerPays" in prev:
            g0, g1 = _xrp_or_token(prev["TakerGets"], rlusd_hex, issuer), _xrp_or_token(fin["TakerGets"], rlusd_hex, issuer)
            p0, p1 = _xrp_or_token(prev["TakerPays"], rlusd_hex, issuer), _xrp_or_token(fin["TakerPays"], rlusd_hex, issuer)
            if None in (g0, g1, p0, p1) or {g0[0], p0[0]} != {"XRP", "RLUSD"}:
                continue
            gets, pays = g0[1] - g1[1], p0[1] - p1[1]
            if gets <= 0 or pays <= 0:
                continue
            if g0[0] == "RLUSD":   # maker gave RLUSD for XRP: a bid; the taker sold XRP
                side, rl, xrp = "sell_xrp", gets, pays
            else:                  # maker gave XRP: an ask; the taker bought XRP
                side, rl, xrp = "buy_xrp", pays, gets
            out.append(Trade(h, node.get("LedgerIndex", ""), li, "book", side, rl / xrp, xrp, rl,
                             fin.get("Account"), ttype))

        elif amm_account and typ == "AccountRoot" and fin.get("Account") == amm_account and "Balance" in prev:
            amm_xrp = (D(prev["Balance"]) / DROPS_PER_XRP, D(fin["Balance"]) / DROPS_PER_XRP)

        elif amm_account and typ == "RippleState" and "Balance" in prev:
            parties = {fin.get("HighLimit", {}).get("issuer"), fin.get("LowLimit", {}).get("issuer")}
            if parties == {amm_account, issuer} and fin.get("Balance", {}).get("currency") == rlusd_hex:
                amm_rl = (abs(D(prev["Balance"]["value"])), abs(D(fin["Balance"]["value"])))

    if amm_xrp and amm_rl and ttype not in NOT_SWAPS:
        dx, dr = amm_xrp[1] - amm_xrp[0], amm_rl[1] - amm_rl[0]
        if dx != 0 and dr != 0 and (dx > 0) != (dr > 0):
            side = "sell_xrp" if dx > 0 else "buy_xrp"   # pool gained XRP: the taker sold XRP
            xrp, rl = abs(dx), abs(dr)
            out.append(Trade(h, "amm", li, "amm", side, rl / xrp, xrp, rl, amm_account, ttype,
                             amm_rl[0] / amm_xrp[0], amm_rl[1] / amm_xrp[1], amm_xrp[0], amm_rl[0]))
    return out


def totals(trades: list[Trade]) -> dict[str, Decimal]:
    return {"xrp": sum((t.xrp for t in trades), ZERO), "rlusd": sum((t.rlusd for t in trades), ZERO)}
