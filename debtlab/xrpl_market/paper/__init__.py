"""RLUSD/XRP paper trading (M7 steps 5-6). PAPER: no key, no wallet, no order on any ledger.

    db.py        the paper database (its own file), its audit events, limits and bot state
    market.py    what the bot knows now, read from the monitor's database (read-only)
    strategy.py  the paper strategy: fair value from the live book, half-spread in parts
    fills.py     the two fill rules of decision E, pure functions
    account.py   the two paper accounts: balances, reserve, value, P&L split, markouts
    risk.py      the paper risk check: the brief's live limits in euro
    venue.py     PaperVenue (a `Venue`), placing and cancelling, network fees
    controls.py  pause, kill switch and reset, persisted
    bot.py       one tick: read the monitor, fill, check safety, decide, requote

The monitor writes the market data; the paper bot only reads it, in its own process, and
writes only its own database, so a paper run never disturbs the monitor.
"""

LABEL = "PAPER"
PAIR = "XRP/RLUSD"
RULES = ("conservative", "optimistic")
