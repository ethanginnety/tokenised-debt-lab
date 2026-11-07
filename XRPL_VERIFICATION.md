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
| Testnet issuer (not used in M7) | `rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV` | Ripple docs, same page. | Checked for M8 on 2 Nov: see *M8: testnet* below. |

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
- **AMM fee.** Voted by liquidity providers, 0% to 1% in steps of 0.001%, so `trading_fee` is in units of 0.001% (1/100,000 as a fraction): 214 = 0.214%.
- **AMM swap formula, checked on the ledger.** XLS-30 (status Final), eq. 9 and 10 with equal weights: a constant product with the fee charged on what goes in. Five real swaps captured on 1 Nov 2026 match eq. 9 at 0.214% to within 1.1e-7, every difference in the pool's favour (rippled's rounding); `tests/test_xrpl_trades.py`.
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
- Depth from the tested book adapter (step 2; `python scripts/xrpl_probe.py --report-only tests/fixtures/real/xrpl/2026-11-01`): within 10 bp of mid, 1,224 RLUSD of bids and 1,828 of asks; within 50 bp, 49,911 and 96,884, far above decision C's €100. Taking 10 RLUSD costs 6.64 bp against the mid on the book either way, against 11.77 (buy) and 31.12 (sell) bp at the AMM.

## M8: testnet (step 1, 2 Nov 2026)

**Result: GO for building against testnet,** with the open items below. Read-only: public read methods only, no key, nothing signed or sent. Raw responses are in [`tests/fixtures/real/xrpl_testnet/2026-11-02/`](tests/fixtures/real/xrpl_testnet/2026-11-02/), checked offline by `tests/test_xrpl_testnet_verify.py`. Reproduce with `scripts/xrpl_testnet_probe.py`.

| Fact | Value | Source |
|---|---|---|
| Network | `network_id` 1, rippled 3.4.1, ledger 22015119 | `server_info` |
| Endpoint | `wss://` and `https://testnet.xrpl-labs.com/` (port 443) | Answered here. **It is the only testnet endpoint reachable from this network:** xrpl.org's `s.altnet.rippletest.net` timed out on ports 51233, 51234 and 443, as did `clio.altnet` on 51233, so there's no second endpoint yet, and its answers couldn't be compared with another server's |
| RLUSD issuer | `rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV`, currency code as on mainnet | [Ripple docs](https://docs.ripple.com/products/stablecoin/developer-resources/rlusd-on-the-xrpl); on the ledger: `Domain` ripple.com, master key disabled, clawback on, no transfer fee, tick size 7 (mainnet sets none, so testnet rounds prices differently), 1,254,511,280.91 outstanding. Not in ripple.com's toml, so unlike mainnet there's no two-way domain check: Ripple's docs and the ledger are the two confirmations |
| Reserves and fee | 1 + 0.2 XRP, 10 drops: same as mainnet | `server_state` |
| Liquidity | AMM 1,472,269 XRP + 117,874 RLUSD; book 6 and 100 offers | **About 0.08 RLUSD per XRP, against about 1.25 on mainnet.** Testnet proves plumbing, never profit |

**`xrpl-py` 5.2.0, pinned by tests with a throwaway in-memory key:**

- A signed offer carries no `NetworkID`, so nothing in it ties it to testnet: hence separate keys, the refusal if the account exists on mainnet, and a `LastLedgerSequence` on every transaction (testnet's ledger numbers, about 22 million, are far below mainnet's, about 108 million).
- `Wallet` hides the seed from `str` and `repr` but **not** from `vars()`: the signer must hide its wallet and never log its attributes.
- It signs whatever it's given, even an XRP-for-XRP offer: our own code must check the pair.
- `tfPassive`, `Expiration`, `OfferSequence` and `LastLedgerSequence` all reach the signed transaction.

**Book mechanics ([OfferCreate](https://xrpl.org/docs/references/protocol/transactions/types/offercreate), [offers](https://xrpl.org/docs/concepts/tokens/decentralized-exchange/offers)):** `tfPassive` skips only offers at exactly the same price and still consumes better-priced ones; there is no post-only flag; offers can also execute against the AMM ([AMMs](https://xrpl.org/docs/concepts/tokens/decentralized-exchange/automated-market-makers)).

### Self-expiring offers: the 30 s budget

[Close times](https://xrpl.org/docs/concepts/ledgers/ledger-close-times) are rounded, "currently 10 seconds", and forced to increase by 1 s when rounding would repeat one. In 60 ledgers on each network every resolution was 10 and every `close_flags` 0, and close times bunch just after each boundary (seconds ending 0, 1, 2 and occasionally 3). An offer is expired once its Expiration is at or before the **previous** ledger's close time, so the first ledger whose close time reaches the Expiration is the last that can execute it.

Measured from the monitor's recording of mainnet, 1-2 Nov (4,639 boundaries by the final run; the rule's constants were fixed at 4,596 and didn't change). All times are when the ledger was seen validated, which runs later than the true close, so they lean cautious:

| Measure | Median | Worst |
|---|---|---|
| Last ledger able to execute, seen after the Expiration | 0.51 s | 9.39 s |
| Close time ahead of our receipt of that ledger | | 2.54 s |

Five-minute live samples (30 boundaries each) stayed inside this: testnet worst 2.39 s, mainnet 4.61 s.

**The rule:** Expiration = the latest 10 s boundary at most 15 s after the latest validated close time, computed from ledger time, not this machine's clock.

**Worst case, from receiving the data an offer was priced from:** 15 + 9.39 + 2.54 = **26.93 s**, leaving about 3 s inside the 30 s limit.

It is a measurement over about 17 hours, not a guarantee, so:

- Quoting stops and everything is cancelled if a ledger's close-time resolution isn't 10 s (it is documented only as "currently") or it closed without consensus on its time (`close_flags` not 0).
- The measurement is repeated on the checkpoint data, and again before any mainnet plan.
- A network that stops closing ledgers can't fill an offer, since only a closing ledger executes anything.

**What it costs:** an offer rests about 10 s or less, so each side is replaced about every 10 s even if nothing moves, losing queue position each time. In network fees that's about 17,280 transactions a day, or 0.173 XRP (about €0.19) on mainnet. This replaces the earlier rough estimate of €0.10 a day at a 20 s refresh, which didn't allow for the rounding.

### M8 open items

- **Replace behaviour on the ledger:** the docs say `OfferCancel` of a missing offer succeeds, but not what `OfferCreate` does when its `OfferSequence` names a missing offer, or whether the old offer is still removed when the new one fails. Observed in step 8; until then the bot trusts only reconciliation against the ledger.
- **A second testnet endpoint: found 2 Nov.** `clio.altnet.rippletest.net` answers on port 443 (not 51233 or 51234; `s.altnet` still doesn't answer on any port tried). `network_id` 1, and the same ledger hash as `testnet.xrpl-labs.com` at ledger 22018699. It answers every read the bot uses, with the same close-time fields. **Its `book_offers` reply has no `validated` field,** so `at` refuses it, and that check isn't relaxed: the bot's own reads and sends can move to Clio, but the monitor stays on `testnet.xrpl-labs.com`. If that endpoint is lost, the bot's data goes stale, so it cancels and stops quoting. **Sending through Clio is untested:** it needs a signed transaction, so step 8 has to show it.
- **Real `account_tx` replies: checked 2 Nov.** On both endpoints: `meta`, `tx` and `validated`, with the hash and ledger inside `tx`, as the fake ledger assumes. The parser read 10 real rows from each, all Payments. Offer transactions on this account are still step 8's to show. With one endpoint, losing it means the bot goes quiet and offers expire by themselves.
- **Xaman and the key file:** Xaman's [testnet guide](https://help.xaman.app/app/learning-more-about-xaman/how-to-access-testnet-on-xrp-ledger) covers developer mode and funding, but not exporting or importing a secret. Checked: Xaman can't export a secret but can import a family seed, so the RUNBOOK starts from the xrpl.org faucet's secret and imports it into Xaman. Not yet tried end to end: Ethan's steps will show.
- **Testnet account size:** that guide funds 1,000 XRP, about 80 RLUSD or €72 at testnet's price, above the €50 total. Settled by Ethan on 2 Nov: the bot isn't refused such an account, it uses €50 and sets the rest aside (ASSUMPTIONS). What the xrpl.org faucet pays out no longer matters.
- **The M7 paper fees don't include the 10 s refresh:** paper charges a fee per requote, so its fees run up to about €0.19 a day under live. To be reported alongside the paper results; strategy values aren't affected.

## M8: mainnet live reads and sends (7 Nov 2026)

Checked read-only from the laptop with the bot's own read code (`MainnetLedger` over `FailoverReads`), before any key existed. Nothing was signed or sent.

- **Network ID:** `server_info` reports `network_id` 0 on `xrplcluster.com` (rippled 3.4.1), `s1.ripple.com` and `s2.ripple.com`. The mainnet guard requires 0 before every submit, as testnet's requires 1.
- **The bot's reads, on an ordinary RLUSD holder** (`rUMBUV11...GF2V`, one trust line, two offers): `ledger`, `account_info`, `account_lines`, `account_offers`, `account_objects` and `account_tx` all answered with validated data on `s2.ripple.com` and `xrplcluster.com`, and the two agreed exactly at ledger 108173924. `account_objects` was added to the read client's public methods for the start-up check.
- **`s1.ripple.com` left out:** at the validated ledger index it had just reported, it answered `ledger` with `lgrNotFound`, twice. It appears to balance across servers that lag one another. Not in the bot's endpoints or the mainnet allowlist.
- **`xrplcluster.com` has a per-IP quota:** 2,000 units per 10 s and 10,000 per 60 s. Paging a long history (500 ledgers of an active account; the issuer's owner directory) used it up, with the mainnet monitor reading from the same IP. The bot's reads and sends go to `s2.ripple.com` first and fall back to `xrplcluster.com`, so they don't share the monitor's quota. The bot reads only its own transactions, a few per ledger.
- **The issuer's flags** at ledger 108173924: `0x819a0000` (clawback allowed, deposit authorisation, default ripple, master key disabled, XRP disallowed, destination tag required); no global freeze. The start-up check refuses a global freeze; the monitor warns on any change (risk R16).
- **Offer expiry on mainnet** uses testnet's rule unchanged: its values were measured on mainnet close-time boundaries (1-2 Nov), worst-case live time 60 s. The close-flag measurement (ASSUMPTIONS) stands at one event in 5,200 ledgers; the shadow run is the first live check of both.

## Still open

- RLUSD's own USD price can't be observed on this pair; the €-limits assume 1 RLUSD = $1 (decision D, risk R19).
- The issuer's `TickSize`, transfer fee and freeze state can change at any time: the monitor rechecks them (step 3).
- Step 4 checkpoint: decision C over at least 2 days.
