"""M7 step 1: the checks behind the go/no-go, as pure functions on raw ledger responses.

Prices are RLUSD per XRP throughout. On the ledger an Offer's owner gives TakerGets and
wants TakerPays, so:
    TakerGets RLUSD, TakerPays XRP  ->  the owner buys XRP: a **bid** for XRP
    TakerGets XRP,   TakerPays RLUSD ->  the owner sells XRP: an **ask** for XRP
Only funded amounts count: `taker_gets_funded` / `taker_pays_funded` when the server
reports the offer as partly funded, else the offer's own amounts.

The depth figures here are a first look for the go/no-go. The tested book adapter with
depth bands and the cost of a €10 trade is step 2.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal

from debtlab.money import D, ZERO

DROPS_PER_XRP = Decimal(1_000_000)


def amount(a: str | dict) -> tuple[str, Decimal]:
    """(currency, value) of a ledger amount: a string of drops is XRP; an object is a token."""
    if isinstance(a, str):
        return "XRP", D(a) / DROPS_PER_XRP
    return a["currency"], D(a["value"])


# --- Issuer ----------------------------------------------------------------------------

def toml_tokens(toml_text: str) -> list[dict]:
    """The [[TOKENS]] and [[ISSUERS]] a domain publishes in its xrp-ledger.toml."""
    data = tomllib.loads(toml_text)
    out = [{"issuer": t.get("issuer"), "currency": t.get("currency"), "name": t.get("name")}
           for t in data.get("TOKENS", [])]
    out += [{"issuer": i.get("address"), "currency": i.get("name"), "name": None}
            for i in data.get("ISSUERS", [])]
    return out


def listed_in_toml(toml_text: str, address: str, currency: str) -> bool:
    """Two-way domain check, the other half of the account's own `Domain` claim.

    An account can set its Domain field to anything; only the domain's own
    xrp-ledger.toml listing the address as the token's issuer confirms it."""
    return any(t["issuer"] == address and t["currency"] == currency for t in toml_tokens(toml_text))


def decode_domain(hex_domain: str | None) -> str | None:
    return bytes.fromhex(hex_domain).decode("ascii") if hex_domain else None


@dataclass(frozen=True)
class IssuerSettings:
    address: str
    ledger_index: int
    domain: str | None
    flags: dict[str, bool]        # as the server reports them (account_flags)
    transfer_fee_pct: Decimal     # 0 when no TransferRate is set
    tick_size: int | None         # None: no issuer TickSize, so 15 significant digits


def issuer_settings(account_info: dict) -> IssuerSettings:
    d = account_info["account_data"]
    rate = d.get("TransferRate")
    fee = ZERO if not rate else (D(rate) - D(1_000_000_000)) / D(10_000_000)
    return IssuerSettings(
        address=d["Account"], ledger_index=int(account_info["ledger_index"]),
        domain=decode_domain(d.get("Domain")), flags=dict(account_info.get("account_flags", {})),
        transfer_fee_pct=fee, tick_size=d.get("TickSize"))


def obligations(gateway_balances: dict, currency_hex: str) -> Decimal:
    """Total of a token the issuer has outstanding."""
    return D(gateway_balances.get("obligations", {}).get(currency_hex, "0"))


# --- Network costs ---------------------------------------------------------------------

@dataclass(frozen=True)
class NetworkCosts:
    ledger_index: int
    reserve_base_xrp: Decimal
    reserve_inc_xrp: Decimal       # per owned object: trust line, offer, ...
    base_fee_drops: Decimal
    current_fee_drops: Decimal     # base fee scaled by the server's load


def network_costs(server_state: dict) -> NetworkCosts:
    s = server_state["state"]
    vl = s["validated_ledger"]
    base_fee = D(vl["base_fee"])
    return NetworkCosts(
        ledger_index=int(vl["seq"]),
        reserve_base_xrp=D(vl["reserve_base"]) / DROPS_PER_XRP,
        reserve_inc_xrp=D(vl["reserve_inc"]) / DROPS_PER_XRP,
        base_fee_drops=base_fee,
        current_fee_drops=base_fee * D(s["load_factor"]) / D(s["load_base"]))


# --- Book ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Side:
    side: str                       # "bid" or "ask" for XRP
    ledger_index: int
    offers: int                     # returned by the server
    funded_offers: int              # with something left to trade
    xrp: Decimal                    # funded XRP on this side
    rlusd: Decimal                  # funded RLUSD on this side
    best: Decimal | None            # best funded price, RLUSD per XRP
    complete: bool                  # False if there may be more offers than were read


def book_side(book_offers: dict, rlusd_hex: str, page_limit: int | None = None) -> Side:
    """`page_limit`: the limit asked for. A last page that comes back exactly full may have
    been cut off even without a marker (servers cap book_offers), so it isn't called complete."""
    offers = book_offers.get("offers", [])
    xrp = rlusd = ZERO
    best: Decimal | None = None
    funded = 0
    side = None
    for o in offers:
        gets_cur, gets = amount(o.get("taker_gets_funded", o["TakerGets"]))
        pays_cur, pays = amount(o.get("taker_pays_funded", o["TakerPays"]))
        this = "bid" if gets_cur == rlusd_hex else "ask"
        if {gets_cur, pays_cur} != {"XRP", rlusd_hex}:
            raise ValueError(f"offer {o.get('index')} is not RLUSD/XRP")
        if side and this != side:
            raise ValueError("offers from both sides of the book in one response")
        side = this
        if gets <= 0 or pays <= 0:
            continue
        funded += 1
        x, r = (pays, gets) if this == "bid" else (gets, pays)
        xrp += x
        rlusd += r
        price = r / x
        if best is None or (price > best if this == "bid" else price < best):
            best = price
    return Side(side or "unknown", int(book_offers["ledger_index"]), len(offers), funded, xrp, rlusd,
                best, "marker" not in book_offers and not (page_limit and book_offers.get("last_page_size") == page_limit))


@dataclass(frozen=True)
class Amm:
    ledger_index: int
    account: str
    xrp: Decimal
    rlusd: Decimal
    price: Decimal                 # pool price, RLUSD per XRP, before its fee
    trading_fee_pct: Decimal       # trading_fee is in units of 0.001%


def amm(amm_info: dict, rlusd_hex: str) -> Amm:
    a = amm_info["amm"]
    pool = dict([amount(a["amount"]), amount(a["amount2"])])
    return Amm(int(amm_info["ledger_index"]), a["account"], pool["XRP"], pool[rlusd_hex],
               pool[rlusd_hex] / pool["XRP"], D(a["trading_fee"]) / D(1000))


@dataclass(frozen=True)
class GoNoGo:
    go: bool
    reasons: list[str]


def go_no_go(bids: Side, asks: Side, *, same_ledger: bool) -> GoNoGo:
    """Step 1's stop point: does the ledger's own order book have funded offers on both
    sides, read from one validated ledger? Whether the depth is *enough* is judged at the
    step 4 checkpoint against decision C, over days, not from one snapshot."""
    reasons = []
    if not same_ledger:
        reasons.append("book sides and the issuer were not all read from the same validated ledger")
    for s in (bids, asks):
        if s.funded_offers == 0:
            reasons.append(f"no funded offers on the {s.side} side")
    if bids.best is not None and asks.best is not None and bids.best >= asks.best:
        reasons.append(f"book crossed: best bid {bids.best} >= best ask {asks.best}")
    return GoNoGo(not reasons, reasons)
