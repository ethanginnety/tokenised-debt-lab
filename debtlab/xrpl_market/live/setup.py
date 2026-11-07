"""The mainnet start-up checks (M8, plan approved by Ethan on 7 Nov 2026). READ-ONLY: it lists problems.

Before the mainnet bot does anything, the account must be exactly as the RUNBOOK sets it up:

    its regular key on the validated ledger is the bot's key
    exactly one trust line: RLUSD from the verified issuer, limit within the agreed 100, not frozen
    it owns nothing but that line and offers: no signer list (someone else could sign), escrow,
        check, payment channel, ticket or anything else
    no offer on the ledger that the bot didn't place: it's left alone, and the bot doesn't start
    the whole account worth no more than `max_account_eur` (the bot uses `max_total_eur` of it)
    the RLUSD issuer has no global freeze on

Any problem refuses the start, before anything is sent or cancelled. Each is said in full.
"""

from __future__ import annotations

from decimal import Decimal

from debtlab.money import D
from debtlab.xrpl_market.live.ledger import Setup

ALLOWED_OBJECTS = frozenset({"RippleState", "Offer"})
LSF_GLOBAL_FREEZE = 0x00400000
WHY = {
    "SignerList": "a signer list lets other keys sign for the account",
    "DepositPreauth": "it changes who can pay the account",
}


class SetupRefused(Exception):
    """The account isn't set up as the RUNBOOK says. Nothing was sent or cancelled."""


def problems(s: Setup, cfg: dict, key_address: str, offers_on_ledger: list[int], known_offers: set[int],
             worth_eur: Decimal | None) -> list[str]:
    out: list[str] = []
    rlusd_hex, issuer = cfg["rlusd"]["currency_hex"], cfg["rlusd"]["issuer"]
    if s.regular_key != key_address:
        out.append(f"the account's regular key on the ledger is {s.regular_key or 'not set'}, not the bot's key "
                   f"{key_address}")
    if not s.lines:
        out.append(f"no RLUSD trust line: add one to {issuer} in Xaman (RUNBOOK)")
    else:
        found = [f"{ln.get('currency')} from {ln.get('account')}" for ln in s.lines]
        ours = [ln for ln in s.lines if ln.get("currency") == rlusd_hex and ln.get("account") == issuer]
        if len(s.lines) != 1 or not ours:
            out.append(f"the account must have exactly one trust line, RLUSD from {issuer}; it has: {'; '.join(found)}")
        for ln in ours:
            cap = D(cfg["live"]["trust_line_limit_rlusd"])
            if not D(ln.get("limit", "0")) > 0 or D(ln.get("limit", "0")) > cap:
                out.append(f"the RLUSD trust line's limit is {ln.get('limit')}; it must be above 0 and at most {cap}")
            if ln.get("freeze") or ln.get("freeze_peer"):
                out.append("the RLUSD trust line is frozen")
    for kind in sorted(set(s.objects) - ALLOWED_OBJECTS):
        why = WHY.get(kind, "the bot expects only its trust line and its own offers")
        out.append(f"the account owns a {kind} object: {why}; remove it in Xaman, or tell Claude")
    strangers = sorted(set(offers_on_ledger) - known_offers)
    if strangers:
        out.append(f"offer(s) {', '.join(map(str, strangers))} on the ledger weren't placed by the bot; they're left "
                   "alone. Cancel them in Xaman, or tell Claude")
    cap = D(cfg["live"]["max_account_eur"])
    if worth_eur is None:
        out.append("no price or exchange rate to value the account with: is the monitor running?")
    elif worth_eur > cap:
        out.append(f"the account is worth EUR {worth_eur:.2f}, over max_account_eur {cap}: is it the right account?")
    if s.issuer_flags & LSF_GLOBAL_FREEZE:
        out.append(f"the RLUSD issuer {issuer} has a global freeze on")
    return out
