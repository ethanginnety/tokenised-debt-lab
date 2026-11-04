"""M7 step 2: the RLUSD/XRP order book as numbers, from raw `book_offers` responses.

Pure functions, no I/O. Prices are RLUSD per XRP. Sizes are what is funded now.

    parse_book   both sides of one validated ledger -> Book (bids best first, asks best first)
    stats        best bid and ask, mid, spread in bp, depth within bands around the mid,
                 or *invalid* with the reasons (empty side, crossed or locked book)
    walk         what buying or selling a given RLUSD amount of XRP would cost by taking
                 offers in price order: average and worst price, cost against the mid in bp
    amm_*        the AMM pool beside the book (never mixed into the book figures, decision B):
                 its price and what the same trade would cost there
    reference    the prices the bot quotes from: the book without the given account's offers and
                 without expired ones, and the AMM at what it really charges, its fee included

An offer's own amounts give its price exactly (its "quality"); its funded amounts give
how much of it can trade now. Offers with nothing funded are left out.

The AMM swap formulas are XLS-30's equations 9 and 10 with equal weights: a constant
product, with the trading fee charged on what goes into the pool. Rounding inside
rippled is not modelled; the monitor (step 3) will compare these with real swaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from debtlab.money import D, ONE, ZERO
from debtlab.xrpl_market.verify import Amm, amount

BP = Decimal(10_000)
BANDS_BP = (10, 25, 50, 100)


@dataclass(frozen=True)
class Level:
    """One offer, as a taker sees it."""
    price: Decimal      # RLUSD per XRP, from the offer's own amounts
    xrp: Decimal        # funded XRP
    rlusd: Decimal      # funded RLUSD
    owner: str
    offer_id: str
    expiration: int | None = None     # Ripple seconds, if the offer has one


@dataclass(frozen=True)
class Book:
    ledger_index: int
    bids: tuple[Level, ...]          # offers to buy XRP, highest price first
    asks: tuple[Level, ...]          # offers to sell XRP, lowest price first
    bids_complete: bool = True       # False: more bids may exist below the last one read
    asks_complete: bool = True
    dropped: int = 0                 # offers returned with nothing funded


def _level(o: dict, rlusd_hex: str) -> tuple[str, Level | None]:
    gets_cur, gets = amount(o["TakerGets"])
    pays_cur, pays = amount(o["TakerPays"])
    if {gets_cur, pays_cur} != {"XRP", rlusd_hex}:
        raise ValueError(f"offer {o.get('index')} is not RLUSD/XRP")
    side = "bid" if gets_cur == rlusd_hex else "ask"
    _, fgets = amount(o.get("taker_gets_funded", o["TakerGets"]))
    _, fpays = amount(o.get("taker_pays_funded", o["TakerPays"]))
    if gets <= 0 or pays <= 0 or fgets <= 0 or fpays <= 0:
        return side, None
    if side == "bid":   # owner gives RLUSD, wants XRP
        price, xrp, rlusd = gets / pays, fpays, fgets
    else:               # owner gives XRP, wants RLUSD
        price, xrp, rlusd = pays / gets, fgets, fpays
    return side, Level(price, xrp, rlusd, o.get("Account", ""), o.get("index", ""), o.get("Expiration"))


def _complete(result: dict, page_limit: int | None) -> bool:
    if "marker" in result:
        return False
    return not (page_limit and len(result.get("offers", [])) >= page_limit)


def parse_book(bids_pages: list[dict], asks_pages: list[dict], rlusd_hex: str,
               page_limit: int | None = None) -> Book:
    """Both sides from `book_offers` results (one or more pages each), all from one ledger."""
    pages = [*bids_pages, *asks_pages]
    if not bids_pages or not asks_pages:
        raise ValueError("need both sides of the book")
    ledgers = {int(p["ledger_index"]) for p in pages}
    if len(ledgers) != 1:
        raise ValueError(f"book pages come from different ledgers: {sorted(ledgers)}")
    if not all(p.get("validated") is True for p in pages):
        raise ValueError("book pages must come from a validated ledger")
    out: dict[str, list[Level]] = {"bid": [], "ask": []}
    dropped = 0
    for want, side_pages in (("bid", bids_pages), ("ask", asks_pages)):
        for p in side_pages:
            for o in p.get("offers", []):
                side, lvl = _level(o, rlusd_hex)
                if side != want:
                    raise ValueError(f"an {side} offer in the {want} pages")
                if lvl is None:
                    dropped += 1
                else:
                    out[side].append(lvl)
    # The server returns offers best first; sort anyway (stable, so FIFO at a price is kept).
    bids = tuple(sorted(out["bid"], key=lambda lv: -lv.price))
    asks = tuple(sorted(out["ask"], key=lambda lv: lv.price))
    return Book(ledgers.pop(), bids, asks, _complete(bids_pages[-1], page_limit),
                _complete(asks_pages[-1], page_limit), dropped)


@dataclass(frozen=True)
class Depth:
    band_bp: int
    bid_rlusd: Decimal
    ask_rlusd: Decimal
    bid_xrp: Decimal
    ask_xrp: Decimal
    complete: bool      # False: a side was cut off inside this band, so these are lower bounds


@dataclass(frozen=True)
class Stats:
    ledger_index: int
    valid: bool
    reasons: tuple[str, ...]
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid: Decimal | None = None
    spread_bp: Decimal | None = None
    depth: tuple[Depth, ...] = field(default_factory=tuple)
    bid_offers: int = 0
    ask_offers: int = 0


def stats(book: Book, bands: tuple[int, ...] = BANDS_BP) -> Stats:
    reasons = []
    if not book.bids:
        reasons.append("no funded bids")
    if not book.asks:
        reasons.append("no funded asks")
    if reasons:
        return Stats(book.ledger_index, False, tuple(reasons), bid_offers=len(book.bids), ask_offers=len(book.asks))
    bb, ba = book.bids[0].price, book.asks[0].price
    if bb >= ba:
        what = "locked" if bb == ba else "crossed"
        return Stats(book.ledger_index, False, (f"book {what}: best bid {bb} >= best ask {ba}",), bb, ba,
                     bid_offers=len(book.bids), ask_offers=len(book.asks))
    mid = (bb + ba) / 2
    depth = []
    for b in bands:
        lo, hi = mid * (ONE - D(b) / BP), mid * (ONE + D(b) / BP)
        bl = [lv for lv in book.bids if lv.price >= lo]
        al = [lv for lv in book.asks if lv.price <= hi]
        # A cut-off side is still complete within the band if what was read goes past it.
        bid_ok = book.bids_complete or book.bids[-1].price < lo
        ask_ok = book.asks_complete or book.asks[-1].price > hi
        depth.append(Depth(b, sum((lv.rlusd for lv in bl), ZERO), sum((lv.rlusd for lv in al), ZERO),
                           sum((lv.xrp for lv in bl), ZERO), sum((lv.xrp for lv in al), ZERO), bid_ok and ask_ok))
    return Stats(book.ledger_index, True, (), bb, ba, mid, (ba - bb) / mid * BP, tuple(depth),
                 len(book.bids), len(book.asks))


@dataclass(frozen=True)
class Walk:
    """Taking `rlusd` worth of XRP from one side, best price first."""
    action: str          # "buy" XRP (takes asks) or "sell" XRP (takes bids)
    rlusd: Decimal       # RLUSD paid (buy) or received (sell)
    xrp: Decimal         # XRP received (buy) or paid (sell)
    avg_price: Decimal | None
    worst_price: Decimal | None
    cost_bp: Decimal | None   # average price against the mid, as a cost (positive = worse than mid)
    levels: int
    filled: bool         # False: the offers read ran out first


def walk(book: Book, action: str, rlusd: Decimal, mid: Decimal | None = None) -> Walk:
    if action not in ("buy", "sell"):
        raise ValueError("action is 'buy' or 'sell' (XRP)")
    if rlusd <= 0:
        raise ValueError("amount must be positive")
    levels = book.asks if action == "buy" else book.bids
    left, paid, xrp, used, worst = rlusd, ZERO, ZERO, 0, None
    for lv in levels:
        if left <= 0:
            break
        take = min(left, lv.rlusd)
        paid += take
        xrp += take / lv.price
        left -= take
        used += 1
        worst = lv.price
    avg = paid / xrp if xrp else None
    cost = None
    if avg is not None and mid:
        cost = (avg / mid - ONE) * BP if action == "buy" else (ONE - avg / mid) * BP
    return Walk(action, paid, xrp, avg, worst, cost, used, left <= 0)


# --- AMM, beside the book -------------------------------------------------------------

def amm_fee(pool: Amm) -> Decimal:
    """The trading fee as a fraction (0.214% -> 0.00214)."""
    return pool.trading_fee_pct / D(100)


def amm_quotes(price: Decimal, fee_pct: Decimal) -> tuple[Decimal, Decimal]:
    """What the pool would really pay for XRP and charge for it, on the smallest trade: its price less
    and plus its fee. Between the two, nothing trades with it."""
    f = D(fee_pct) / D(100)
    return price * (ONE - f), price * (ONE + f)


@dataclass(frozen=True)
class Reference:
    """The prices to quote from. `mid` is None, with the reasons, when there is no usable price."""
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    reasons: tuple[str, ...]
    own_left_out: int
    expired_left_out: int


def reference(b: Book, pool: Amm | None, close_time: int, exclude: str | None = None) -> Reference:
    """The best bid and offer, leaving out `exclude`'s offers, so the bot can't price from its own quotes, and
    offers expired by this ledger's `close_time` (Ripple seconds), which the ledger lists but can't execute.
    The AMM counts at its prices with the fee: it only moves a price when it beats what's left of the book,
    and it gives a price when a side is empty."""
    own = expired = 0

    def keep(levels: tuple[Level, ...]) -> list[Level]:
        nonlocal own, expired
        out = []
        for lv in levels:
            if exclude and lv.owner == exclude:
                own += 1
            elif lv.expiration is not None and lv.expiration <= close_time:
                expired += 1
            else:
                out.append(lv)
        return out

    bids, asks = keep(b.bids), keep(b.asks)
    amm_bid, amm_ask = amm_quotes(pool.price, pool.trading_fee_pct) if pool else (None, None)
    bid = max((p for p in (bids[0].price if bids else None, amm_bid) if p is not None), default=None)
    ask = min((p for p in (asks[0].price if asks else None, amm_ask) if p is not None), default=None)
    reasons = []
    if bid is None:
        reasons.append("no bid from anyone else and no AMM")
    if ask is None:
        reasons.append("no offer from anyone else and no AMM")
    if bid is not None and ask is not None and bid >= ask:
        reasons.append(f"prices crossed: bid {bid} >= offer {ask}")
    mid = None if reasons else (bid + ask) / 2
    return Reference(bid, ask, mid, tuple(reasons), own, expired)


def amm_walk(pool: Amm, action: str, rlusd: Decimal, mid: Decimal | None = None) -> Walk:
    """Buying XRP with `rlusd`, or selling XRP for `rlusd`, against the pool alone.

    XLS-30 eq. 9 (swap in) for a buy: XRP out = X * (1 - R / (R + rlusd * (1 - fee))).
    XLS-30 eq. 10 (swap out) for a sell: XRP in = X * (R / (R - rlusd) - 1) / (1 - fee).
    X and R are the pool's XRP and RLUSD."""
    if action not in ("buy", "sell"):
        raise ValueError("action is 'buy' or 'sell' (XRP)")
    if rlusd <= 0:
        raise ValueError("amount must be positive")
    f = amm_fee(pool)
    x, r = pool.xrp, pool.rlusd
    if action == "buy":
        xrp = x * (ONE - r / (r + rlusd * (ONE - f)))
    else:
        if rlusd >= r:
            return Walk(action, ZERO, ZERO, None, None, None, 0, False)
        xrp = x * (r / (r - rlusd) - ONE) / (ONE - f)
    avg = rlusd / xrp
    cost = None
    if mid:
        cost = (avg / mid - ONE) * BP if action == "buy" else (ONE - avg / mid) * BP
    return Walk(action, rlusd, xrp, avg, None, cost, 1, True)  # a pool has no 'worst level'
