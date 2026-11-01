# XRPL verification: RLUSD/XRP (M7 step 1)

Brief §7 stage 1: verify identifiers, endpoints, reserves, fees and book mechanics from authoritative sources, confirm that the ledger's own order book has real RLUSD/XRP depth, and stop if it doesn't.

**Result, 1 Nov 2026: GO.** The ledger's own order book has funded offers on both sides, read from one validated ledger. Whether the depth is *enough* is decided at the step 4 checkpoint against decision C (at least 99% of ledgers funded on both sides; median spread at most 50 bp; median depth within 50 bp of mid at least €100 each side), over at least 2 days of monitoring, not from one snapshot.

Everything here was read on 1 Nov 2026, read-only: public read methods only, no keys, no wallet. The raw ledger responses are saved byte for byte in [`tests/fixtures/real/xrpl/2026-11-01/`](tests/fixtures/real/xrpl/2026-11-01/), with a manifest, and `tests/test_xrpl_verify.py` checks them. To repeat: `python scripts/xrpl_probe.py` (saves a new dated folder and prints the report) or `python scripts/xrpl_probe.py --report-only tests/fixtures/real/xrpl/2026-11-01`.

## How a fact counts as verified

Two independent confirmations: Ripple's or the XRP Ledger's own published source, **and** the ledger itself (or the issuer's domain). Where they disagree, the ledger wins for what the account *is* now, and the disagreement is recorded below.

## RLUSD identifiers

| Fact | Value | Published source | Ledger / domain check |
|---|---|---|---|
| Issuer (mainnet) | `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De` | Ripple docs, [RLUSD on the XRP Ledger](https://docs.ripple.com/products/stablecoin/developer-resources/rlusd-on-the-xrpl) ("Issuer account address") and [Token addresses](https://docs.ripple.com/products/stablecoin/overview/token-addresses) ("For XRPL: Issuer address"). Both agree. | Account exists; `Domain` = `https://ripple.com/`; **and** `https://ripple.com/.well-known/xrp-ledger.toml` lists it under `[[ISSUERS]]` (name "RLUSD") and `[[TOKENS]]` (currency "RLUSD", name "Ripple USD"). Two-way confirmed. |
| Currency code | `524C555344000000000000000000000000000000` (hex of "RLUSD", padded to 40 hex characters: codes longer than 3 characters must be hex) | Ripple docs, same page. | The issuer's only obligation in that code: 711,828,559.43 RLUSD outstanding at ledger 108053087 (`gateway_balances`). |
| Testnet issuer (not used in M7) | `rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV` | Ripple docs, same page. | Not checked (mainnet read-only only). |

### A look-alike issuer: do not use

A web search turned up a second address, `rLUSDtykL2NVz3HJe1Jqoc7dsxWFVcsmuK`, shown by an explorer as an "RLUSD issuer". On the ledger it issues a token with **exactly RLUSD's currency code** and sets its `Domain` to `https://ripple.com/`, but:

- it is **not** in ripple.com's `xrp-ledger.toml` (the `Domain` field is set by the account itself, so on its own it proves nothing);
- Ripple's docs don't mention it;
- its settings are unlike a regulated stablecoin issuer's: master key enabled, clawback not enabled, `noFreeze` set (it has permanently given up freezing);
- about 999,992,178 of its token are outstanding, 997,601,903 of them on one trust line (`rLUSDemWn5uvqkLXWu5XomNAaWb6EAEJVM`), first active 12 Jun 2024; it has its own small book and AMM pool.

It is recorded in `config/xrpl.json` as a known look-alike, and every probe checks that it is still not listed. **Lesson for the code:** a token is identified by issuer *and* currency, and the issuer by a two-way domain check, never by currency code or `Domain` alone.

## Issuer settings (ledger 108053087)

From `account_info` on the ledger, compared with Ripple's [RLUSD XRPL settings](https://github.com/ripple/RLUSD-Implementation/blob/main/doc/rlusd-xrpl-settings.md) document (undated).

| Setting | On the ledger | Ripple's document | Matters for M7 |
|---|---|---|---|
| Clawback (`allowTrustLineClawback`) | on | enabled | The issuer can take RLUSD back from any holder (risk R16). |
| Freeze | `globalFreeze` off; `noFreeze` off (the issuer keeps the power to freeze) | "lsfGlobalFreeze ✅ Enabled" | The document's "enabled" reads as *available*; the ledger shows no freeze in force. R16. |
| Transfer fee (`TransferRate`) | none: 0% | not listed | No transfer fee on RLUSD moves, so none in paper P&L. Rechecked by the monitor. |
| Tick size (`TickSize`) | **not set**: offers keep 15 significant digits | "lsfTickSize ✅ Enabled" | **Disagreement.** The ledger wins: no rounding of offer rates by the issuer today. Rechecked by the monitor, since the issuer can set it at any time. |
| Require destination tag | **on** | "❌ Not enabled" | **Disagreement.** Affects payments *to the issuer* only, not trading. |
| Deposit authorisation | on | enabled | Issuer only accepts authorised deposits; no effect on trading. |
| Default ripple | on | enabled | Needed for holders to trade RLUSD with each other. |
| Disallow XRP | on | enabled | Advisory; no effect on trading. |
| Require authorisation | off | not enabled | Anyone can hold RLUSD; no approval needed for a trust line. |
| Master key | **disabled** | not listed | The issuer signs with another method (regular key or multi-signing), not its master key. |

## Network costs (ledger 108053087)

| Fact | Value | Published source | Ledger check |
|---|---|---|---|
| Base reserve | 1 XRP | [xrpl.org: Reserves](https://xrpl.org/docs/concepts/accounts/reserves) (mainnet 1 XRP base, 0.2 XRP per owned object; can change by validator fee voting) | `server_state` `validated_ledger.reserve_base` = 1,000,000 drops |
| Owner reserve | 0.2 XRP per object (each trust line, each resting offer) | same | `reserve_inc` = 200,000 drops |
| Transaction cost | 10 drops (0.00001 XRP) reference; scales with load; destroyed, not paid to anyone; charged even for a transaction that fails with a `tec` code | [xrpl.org: Transaction cost](https://xrpl.org/docs/concepts/transactions/transaction-cost) | `base_fee` = 10, load factor 256/256 (1×) |

What a live account would hold back (for paper decision D): 1 XRP base + 0.2 per trust line (one, to RLUSD) + 0.2 per resting offer (two at most when quoting both sides) = **1.6 XRP**, about 2 RLUSD at the snapshot's mid. Reserves are read from each validated ledger, not hard-coded, because validators can vote to change them (archived versions of the xrpl.org page show earlier levels of 20 and 5 XRP, then 10 and 2 XRP).

## Order book and AMM mechanics

From xrpl.org, read 1 Nov 2026: [Offers](https://xrpl.org/docs/concepts/tokens/decentralized-exchange/offers), [AMMs](https://xrpl.org/docs/concepts/tokens/decentralized-exchange/automated-market-makers), [Tick size](https://xrpl.org/docs/concepts/tokens/decentralized-exchange/ticksize), [Transfer fees](https://xrpl.org/docs/concepts/tokens/transfer-fees), [book_offers](https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/path-and-order-book-methods/book_offers), [subscribe](https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/subscription-methods/subscribe).

- **An offer's owner gives `TakerGets` and wants `TakerPays`.** So for RLUSD/XRP: TakerGets RLUSD / TakerPays XRP is a *bid* for XRP; TakerGets XRP / TakerPays RLUSD is an *ask*. Prices here are RLUSD per XRP.
- **Funding.** "Placing an Offer does not lock up your funds." An offer can lose its funding and stays on the ledger until something removes it. `book_offers` "omits unfunded offers and reports how much of each remaining offer's total is currently funded" (`taker_gets_funded`, `taker_pays_funded` on partly funded offers). In practice the response still contained offers with nothing left to trade: 38 of the 400 bids in the saved snapshot (33 funded to exactly zero, 5 whose funded amount on one side is zero). The code counts funded amounts and skips these.
- **Priority.** Better rate first; at the same rate, first in, first out ("offers that have the same quality are automatically taken first-in, first-out"). This is why the conservative paper rule doesn't fill us on trades exactly at our price: we'd be at the back of that queue.
- **Offer crossing and the AMM.** "A single transaction might execute by matching offers, AMMs, or a mix of both, depending on what's cheaper", and offers and payments "are automatically optimized" across the pool and the books. So a taker going through the AMM at a price worse than a resting offer would have taken that offer first: the basis for counting AMM-routed trades in the paper fill rules (decision B, Ethan's change).
- **`book_offers` doesn't include AMM liquidity** (it lists Offer objects only; the AMM is a separate ledger entry read with `amm_info`). Inferred from the docs and consistent with the data: the pool's price is not among the book's offers.
- **AMM fee.** Voted by liquidity providers, 0% to 1% in steps of 0.001%, so `trading_fee` is in units of 0.001%: 214 = 0.214%.
- **Tick size** truncates an offer's rate to 3–15 significant digits when it rests on the book; set by the issuer (RLUSD: not set).
- **Resting offer flags.** *Passive* doesn't take offers at exactly its own rate (it still takes better ones); *Immediate or cancel* and *Fill or kill* never rest. Relevant only from M8.
- **Paging.** Each side of the book returned exactly the 400-offer limit **without a continuation marker**. A full page is therefore treated as possibly incomplete (the code says so); the offers that weren't returned are further from the mid, so best prices and near-mid depth aren't affected.
- **Streams (for step 3).** The `ledger` stream sends a message only when a ledger is validated. A `books` subscription (`both`, `snapshot`) sends transactions affecting the book; the docs don't say outright that these are validated-only, so the monitor won't rely on stream contents for state: on each validated ledger it re-reads the book and the AMM at that ledger index.

## Endpoints

| Endpoint | Operator | Notes |
|---|---|---|
| `https://xrplcluster.com/`, `wss://xrplcluster.com/` (primary) | XRPL Foundation infrastructure (InFTF) | Listed by Ripple's RLUSD page and [xrpl.org public servers](https://xrpl.org/docs/tutorials/public-servers): "Full history server cluster with CORS support". |
| `https://s2.ripple.com:51234/`, `wss://s2.ripple.com/` (fallback) | Ripple | Listed on xrpl.org and in ripple.com's toml. Ripple's servers are "not for sustained or business use, and they may become unavailable at any time". |

No numeric rate limits are published. The monitor reads a handful of requests per validated ledger (every 3–4 s), well within ordinary use, with backoff on errors. Both endpoints answered from this environment on 1 Nov 2026 (rippled 3.4.1), so the probe ran here; Ethan can rerun it on his machine.

## The snapshot (ledger 108053087, 1 Nov 2026 21:15 UTC)

One snapshot, for the go/no-go only. Depth is judged over days at the checkpoint.

| | Bids (buy XRP) | Asks (sell XRP) |
|---|---|---|
| Offers returned / funded | 400 / 362 | 400 / 400 |
| Funded size on the first page | 807,110 XRP for 947,997 RLUSD | 172,479 XRP for 218,992 RLUSD |
| Best price, RLUSD per XRP | 1.248241 | 1.249899 |

- Spread **13.27 bp** around a mid of 1.249070 RLUSD per XRP.
- AMM pool `rhWTXC2m2gGGA9WozUaoMm6kLAVPb1tcS3`: 1,871,457 XRP and 2,335,313 RLUSD, pool price 1.247858, fee 0.214%. Before its fee the pool price sits just below the book's best bid; with the fee, buying from the pool (about 1.2505) costs more than the book's best ask, and selling to it (about 1.2452) gets less than the best bid. So the book is the better price at the top, and the pool is context, as decision B says.
- A rough, untested read of an earlier snapshot (ledger 108053012) found about 51,500 RLUSD of bids and 99,000 of asks within 50 bp of mid, far above decision C's €100. The tested depth bands come with the book adapter (step 2).

## Still open

- RLUSD's own USD price can't be observed on this pair; the €-limits assume 1 RLUSD = $1 (decision D, risk R19).
- The issuer's `TickSize`, transfer fee and freeze state can change at any time: the monitor rechecks them (step 3).
- Step 4 checkpoint: decision C over at least 2 days.
