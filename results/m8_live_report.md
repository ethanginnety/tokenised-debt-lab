# M8 live report: RLUSD/XRP market making on the XRPL mainnet

**MAINNET: real funds.** Account `rH5NrqyAkxt3JXtuyi4XkYRBGkTrB1Hwog`, signed only by its regular key `ravzzNVJTvE8kj13knvY7NJ2ykxPgvRrdm`. Brief §7.5. Written by Claude on 9 Nov 2026 from the live, paper-alongside and monitor databases, read-only.

- The exact figures are in `results/m8_live_report.json`, written by `scripts/xrpl_live_report.py`. It also lists every fill's transaction hash, ledger, side and size.
- The sessions as they happened are in `results/m8_live_sessions.txt`.
- RLUSD is counted as USD. Euro figures use the account's opening rate, 1.1165 USD per EUR (ECB). RISK_REGISTER R19 covers the risk that RLUSD isn't exactly $1.

## Headline

- **Result:**
  - The whole account lost **2.17 RLUSD (about €1.94)**. It went from 65.44 to 63.27 RLUSD between the first session's opening on 7 Nov and the last stop on 9 Nov.
  - XRP fell 5.5% over that time (1.1619 to 1.0985 RLUSD per XRP). Holding the opening balances would have lost 1.99, so the bot did **0.18 RLUSD worse than doing nothing**.
- **While quoting:** the bot lost **0.70 RLUSD** over 7.55 h of quoting. It earned spread of +0.97 and paid fees of 0.07, but lost 1.59 holding inventory while the price moved.
- **While not quoting:** 1.45 RLUSD of the loss came while the bot wasn't quoting:
  - 1.28 overnight between sessions 2 and 3, stopped and holding 28.4 XRP;
  - 0.17 while paused after the two taker fills.
- **Live against paper:** live traded far more than paper on the same quotes: 2,913 XRP against paper's 1,768 to 2,067.
  - Most of the difference is the bot quoting again after its offer filled, while the paper order it was following was still open.
  - The rest is the replacement race in R21.
  - Capped at one paper order's worth per quote, live falls inside paper's range in both budget periods.
- **Safety:**
  - Every fill was checked against the validated ledger by hash: 674 fills, 0 mismatches.
  - Every transaction was signed by the regular key.
  - In session 1, the bot went 1.88 XRP over its budget (R21, fixed before session 2).
  - Its offers took liquidity twice (R20); both times the bot paused within the same ledger.
  - The €5 daily loss limit never came close.
- **Length:** 17.5 h running, 7.55 h quoting. The run ended early, at 7.55 h of uptime out of 24 h by the recorded rule, by Ethan's decision on 9 Nov because of his exams this week, not because of the P&L. See *Uptime and the end of the run* for a correction of mine about how uptime was counted.

These are real fills on mainnet, but over a short run in a falling market. They don't show whether the strategy makes money.

**Status: M8 done.** Ethan agreed with this report on 10 Nov 2026. The fill-by-fill explanation of each live against paper difference is deferred, by his decision, until after his exams (*Live against paper*, *Limitations*).

## The run

| Session | Budget period | Run (UTC) | Run time | Quoting (uptime) | Not quoting while running | How it ended |
|---|---|---|---|---|---|---|
| 1 | 1 | 7 Nov 18:09:14 to 20:39:26 | 2 h 30 min | 2 h 30 min | none (see note) | stopped by Ethan |
| 2 | 2 | 8 Nov 12:09:03 to 21:17:11 | 9 h 08 min | 4 h 36 min | 4 h 31 min paused after a taker fill | stopped by Ethan |
| 3 | 2 | 9 Nov 16:51:13 to 22:43:04 | 5 h 52 min | 27 min | 5 h 23 min paused after a taker fill | stopped by Ethan |
| **Total** | | | **17 h 30 min** | **7 h 33 min (7.55 h)** | | |

*Quoting* is the uptime rule recorded before any live order: time with a quote and no gap over 30 s, from the decisions, as for paper (`results/m8_live_cutoff.txt`).

Note on session 1: from 20:10 its budget had no XRP left, and the risk check refused every live order for the last 29 minutes, so no offers were on the ledger. The strategy was still deciding quotes, so the rule counts that time as uptime.

### Budget periods

- **Period 1** is session 1.
- **Period 2** is sessions 2 and 3. Ethan opened it on 8 Nov after session 1's budget breach: a fresh €50 budget out of what the account held then, on new databases. The rest of the account was set aside.
- The two periods are reported separately. A budget period's P&L counts only its budget; the set-aside amounts are in the whole-account figures.

### Uptime and the end of the run

- **The rule,** exactly as recorded in `results/m8_live_cutoff.txt` on 7 Nov 2026 at 11:31 UTC, before any live order: the run's length is "up to 24 h of uptime, summed across supervised sessions (uptime as for paper: time quoting, with no gap over 30 s), OR the hard stop below, whichever comes first." The hard stop: "Wednesday 11 Nov 2026, 20:00 Irish time = 2026-11-11T20:00:00Z".
- **Both figures:** **17.5 h running** (17 h 30 min, start to stop, three sessions) and **7.55 h quoting** (7 h 33 min, uptime by the rule).
- **My error:** my session log counted uptime as run time, from start to stop, and told Ethan "17.50 h of 24 h". That was wrong. Under the recorded rule the run reached **7.55 h**. The paused time after each taker fill (9 h 54 min) and the time before the first quote don't count.
- **Ethan's decisions, 9 Nov:**
  - Keep the uptime rule as it was: no change after seeing results.
  - End the live run now, because of his exams this week, not the P&L.
- **Status:** the run ends at 7.55 h, short of both the 24 h and the hard stop. The figures below cover everything that ran; nothing came after the end.

## Balances

| On the ledger | XRP | RLUSD | Mid | Value, RLUSD |
|---|---|---|---|---|
| Opening, 7 Nov (ledger 108179565) | 31.40205 | 28.951 | 1.161931 | 65.4380 |
| After session 1 (ledger 108181822) | 2.731365 | 62.2566948904985 | 1.158280 | 65.4204 |
| Opening of period 2, 8 Nov (ledger 108195914) | 2.731365 | 62.2566948904985 | 1.166106 | 65.4418 |
| After session 2 (ledger 108204075) | 28.82183 | 31.64324506462646 | 1.144093 | 64.6181 |
| After session 3, the end (ledger 108226743) | 30.752076 | 29.48669460605546 | 1.098512 | 63.2682 |

The books matched the ledger exactly after every session: each budget plus its set-aside equals the ledger balance.

| Budget | XRP | RLUSD | Value, RLUSD | Set aside |
|---|---|---|---|---|
| Period 1, opening | 26.789002 | 24.6980180514 | 55.8250 | 4.613048 XRP, 4.2529819485999 RLUSD |
| Period 1, end | **-1.881683** | 58.0037129418986 | 55.8242 | (the -1.88 XRP came out of the set-aside: R21) |
| Period 2, opening | 2.329987 | 53.1079877751 | 55.8250 | 0.401378 XRP, 9.1487071153985 RLUSD |
| Period 2, end | 30.350698 | 20.33798749065696 | 53.6786 | unchanged |

How the whole account's -2.1698 RLUSD adds up:

| Part | RLUSD |
|---|---|
| Period 1's budget | -0.0008 |
| Period 1's set-aside (4.61 XRP as the mid fell) | -0.0168 |
| Between periods (7 Nov 20:39 to 8 Nov 12:09, 2.73 XRP as the mid rose) | +0.0214 |
| Period 2's budget | -2.1464 |
| Period 2's set-aside | -0.0271 |
| **Total** | **-2.1698** |

## Orders, fills, cancels, failures, fees

| | Period 1 | Period 2 |
|---|---|---|
| Orders placed (OfferCreate) | 758 | 1,823 |
| Filled | 66 | 276 |
| Cancelled or replaced | 660 | 1,540 |
| Expired on the ledger | 13 | 6 |
| Failed (not applied) | 19 | 1 |
| Refused by the risk check | 24 | 257 |
| Fills | 142: 84 buys (282.07 XRP), 58 sells (310.73 XRP) | 532: 304 buys (1,174.13 XRP), 228 sells (1,146.05 XRP) |
| Fills that took liquidity | 0 | 2 (0.9251 XRP) |
| Own transactions on the ledger | 746 (739 OfferCreate, 7 OfferCancel, all tesSUCCESS) | 1,980 (1,822 OfferCreate, 158 OfferCancel, all tesSUCCESS) |
| Network fees | 0.00746 XRP | 0.055088 XRP |

**Failures:**
- **Period 1:**
  - Of 767 transactions sent, 21 never applied.
    - 12 expired unapplied, past their LastLedgerSequence; 9 of them had been queued first.
    - 9 failed at submit: 7 `telCAN_NOT_QUEUE` or `telCAN_NOT_QUEUE_FEE`, and 2 `tefPAST_SEQ`.
  - 152 were queued (`terQUEUED`) at submit, and 11 were refused at submit with `telCAN_NOT_QUEUE`; 4 of those refused applied anyway. The fee was the monitor's base fee, below the open-ledger cost when the network was busy.
- **Period 2:** after fix 3 (fee from the open-ledger cost plus 20%, LastLedgerSequence 5 ahead), one OfferCreate expired unapplied, and none were refused or left queued.

**Refusals:**
- **Period 1:** sells for lack of budget XRP (16), buys for lack of RLUSD (6), and both sides once the XRP budget was gone (2 logged, repeated for 29 minutes).
- **Period 2:**
  - sells for lack of budget XRP (155), mostly at the start, which was mostly RLUSD;
  - buys for lack of budget RLUSD (101), mostly later, which was mostly XRP;
  - once, a bid within 2 bp of the best offer (the price margin).

## P&L split (RLUSD; EUR at 1.1165)

The P&L is split the same way as for paper:
- **spread:** each fill against the mid at that moment;
- **fees:** network fees;
- **inventory:** the mid's move on what was held.

Inventory is split again by whether the bot was quoting, and when it wasn't, into paused within a session and stopped between sessions. The marks are the mid at every decision, so no stretch of time is counted under the wrong heading.

| | Spread | Fees | Inventory, quoting | Inventory, paused | Inventory, stopped | **Total** | EUR |
|---|---|---|---|---|---|---|---|
| Period 1 (session 1) | +0.1734 | -0.0087 | -0.1656 | 0 | 0 | **-0.0008** | -0.0007 |
| Session 2 | +0.6569 | -0.0579 | -1.0623 | -0.3515 | | **-0.8148** | -0.7298 |
| Stopped, 8 Nov 21:17 to 9 Nov 16:51 | | | | | -1.2787 | **-1.2787** | -1.1453 |
| Session 3 | +0.1382 | -0.0057 | -0.3653 | +0.1800 | | **-0.0529** | -0.0474 |
| Period 2 | +0.7950 | -0.0636 | -1.4276 | -0.1716 | -1.2787 | **-2.1464** | -1.9224 |
| **Both periods** | **+0.9684** | **-0.0722** | **-1.5932** | **-0.1716** | **-1.2787** | **-2.1472** | **-1.9232** |

- **Time quoting vs not:**
  - Quoting: -0.6970 RLUSD over 7.55 h (-0.0008 in period 1, -0.6962 in period 2).
  - Not quoting: -1.4502 RLUSD, period 2 only: -0.1716 paused and -1.2787 stopped.
- **Against doing nothing:**
  - Period 1: holding its opening budget would have lost 0.0978, so the bot did 0.0970 better.
  - Period 2: holding would have lost 0.1575, because the budget opened mostly in RLUSD. The bot did 1.9889 worse: it bought XRP into a 5.8% fall (1.1661 to 1.0985) and held it through the night.
- **Largest drawdown:**
  - Period 1: 0.0904 RLUSD (7 Nov 20:01 to 20:07).
  - Period 2: 2.3869 RLUSD (8 Nov 13:23 to 9 Nov 17:19).
- **Spread captured:**
  - 0.9684 RLUSD on 3,354 RLUSD traded, about 2.9 bp.
  - Paper's M7 run captured 3.31 bp, assuming first place in the queue.
- **Session 3's total:** -0.0529 here against -0.0304 in the session log. The log marked from the day's opening value; this report marks from the session's start. Use this report's figure.

## Live against paper

Paper alongside ran on exactly the same quotes, under both fill rules, on its own accounts. Live followed the conservative account's orders.

| | Period 1 | Period 2 |
|---|---|---|
| XRP filled, live | 592.80 | 2,320.18 |
| XRP filled, paper conservative / optimistic | 348.47 / 399.21 | 1,419.78 / 1,667.40 |
| Live, capped at one paper order's size per quote | **382.33** | **1,571.44** |
| Live beyond one paper order: refills | 138.99 | 463.30 |
| Live beyond one paper order: double-offer race (R21) | 71.48 | 285.43 |
| P&L, live | -0.0008 | -2.1464 |
| P&L, paper conservative / optimistic | -0.0480 / -0.0772 | -1.4950 / -1.3830 |
| Of which while quoting, live | -0.0008 | -0.6962 |
| Of which while quoting, paper conservative / optimistic | -0.0480 / -0.0772 | -0.1966 / -0.0279 |
| Network fees, XRP: live / conservative / optimistic | 0.00746 / 0.00372 / 0.00353 | 0.055088 / 0.01638 / 0.01521 |

**Why live filled more:**

1. **Refills.** A paper order fills at most once, up to its size. Live follows that paper order. When the live offer fills and the paper order is still open (paper hasn't filled it, or only partly), the bot places a new offer for the same paper order, and that offer can fill again. This happened on 33 of 56 filled paper orders in period 1 and 111 of 251 in period 2.
   - It's a real effect of following paper this way: live sells or buys the same quote's worth again in a market moving one way.
   - Paper can't show it.
2. **The replacement race (R21).** A replacement offer goes up while its old offer fills unseen, so one quote's worth trades twice. After the fix the budget held, but the race still happened:
   - period 1: 96.68 XRP in all, 71.48 of it beyond one paper order;
   - period 2: 416.81 XRP in all, 285.43 beyond.
   - Fix 1 stops it breaking the budget. It doesn't stop it happening.
3. **What's left is within paper's range.** Capped at one paper order per quote, live filled 382.33 XRP against paper's 348.47 to 399.21 in period 1, and 1,571.44 against 1,419.78 to 1,667.40 in period 2.
   - Quote by quote, the differences go both ways. Live filled where conservative paper didn't (104.94 and 467.26 XRP), and conservative paper filled where live didn't (90.28 and 424.01 XRP).
   - These are queue position, other traders and timing. I haven't attributed them quote by quote, so the requirement to explain every difference is met in aggregate, not for each fill. Ethan decided on 10 Nov that the quote-by-quote explanations wait until after his exams.
4. **Fees.** Live paid 2 to 3.6 times paper's fees. Live pays every 15 to 25 s to refresh offers that expire by themselves; paper is charged only when the price moves. At 0.07 RLUSD in all, fees are small next to the spread and inventory.

**Why live lost more while quoting in period 2:**
- Live traded more: refills and races, mostly buying into a falling price, so it held more XRP as the price fell.
- Live's budget also opened mostly in RLUSD (2.33 XRP, 53.11 RLUSD). The paper accounts opened half and half (23.94 XRP, 27.91 RLUSD each), so they're not like-for-like on inventory.

## Limit events and incidents

| When (UTC) | What | What happened |
|---|---|---|
| 7 Nov 18:49:49 | Close-time check tripped once (resolution 10 s, flags 1) | Offers cancelled; quoting resumed by itself once the check held again. As designed. |
| 7 Nov, session 1 | **Budget breach (R21)** | The bot sold 1.881683 XRP more than its budget, out of the set-aside (about €2.20). A replacement offer was sized as if its old offer were still whole, while a taker filled that old offer first. This happened 35 times; in 15 both filled. The last one (orders L108070375 and L108070377, both filled in ledger 108181372) took the budget below zero. Nothing paused. The ledger reserve held, and the books matched the ledger. **Fixed before session 2, approved by Ethan, tests first:** an offer being replaced counts in full against the budget and the €50 total until its replacement settles, and a budget below zero pauses the bot. In period 2 the lowest budget was 1.999316 XRP; RLUSD reached 0.00000015, the edge where the fix shrinks the order. It never went below zero. |
| 7 Nov 20:10 to 20:39 | Both sides refused, zero offers | The budget had no XRP even for fees, and nothing paused or alerted. With fix 2, a budget below zero now pauses. |
| 7 Nov, session 1 | Fee escalation | 152 transactions queued and 11 refused at submit; 21 never applied (12 expired, 9 failed). Fixed with fix 3 (above). |
| 8 Nov 16:45:46 | **Took liquidity (R20)**, buy, transaction `EBBA3521119E807DDB9AA8535CFC733526D5F4E149B4FE217DD6960C80AF0AF2` | Priced at 1.156992, below the mid of 1.157286 at the ledger it was sized from. It applied 2 ledgers later after the mid fell to 1.156461, and crossed a resting offer at about 1.15697: 0.4066 XRP taken for 0.470426 RLUSD. Cost against the mid about 0.0002 RLUSD. **Paused in the same ledger, as designed.** It stayed paused until stopped at 21:17. |
| 8 Nov 16:59:32 | Connection lost (DNS failure reading xrplcluster.com) | Recovered by itself; the bot was already paused. |
| 8 Nov 19:58 | Monitor stopped by mistake (Ctrl+C meant for the bot) | The bot ran on, paused, without it until 21:17; no offers. |
| 9 Nov 17:19:46 | **Took liquidity again (R20)**, sell, transaction `381D2D791621B86179289A993FF5B8FF9E9F2536408D3EB4C5F79C594805CB18` | Priced at 1.092636 with the mid at 1.091955. It applied 2 ledgers later after the price jumped, and crossed a bid resting above it: 0.518477 XRP sold for 0.566839 RLUSD, about 1.09328, above the mid, so no cost. **Paused in the same ledger.** It stayed paused until stopped at 22:43. |
| Never | €5 daily loss, €10 order, €50 total (after the fix), 2 open orders, 30 s data age | No breach. The order limit held per offer, though in a race two offers' worth traded on one side. The ledger never held two offers on one side at once. |

The pause on a taker fill is the rule the plan set: "never intends to take, and stops if it does". It worked both times. The margin kept from the best opposite price doesn't cover a move of that size within the 2 ledgers before an offer lands. Over 7.55 h of quoting, that happened twice, so a longer run would have paused every few hours. Ethan decided on 9 Nov not to change the margin during the run; widening it in fast markets is listed in *Ideas after M8* below.

## Checked against the ledger

- **Method:** every transaction of the account from each period's opening ledger to after its last stop was read from a public mainnet endpoint. Balance changes were read straight from the transaction metadata and compared, hash by hash, with the bot's books.
- **Period 1:** ledgers 108179566 to 108181849, 889 transactions.
  - 746 were the account's own and 143 were others crossing its offers.
  - 142 fill hashes in the books, all on the ledger, with 0 mismatches.
- **Period 2:** ledgers 108195915 to 108226743, 2,510 transactions.
  - 1,980 were the account's own and 530 were others crossing its offers.
  - 532 fill hashes in the books, all on the ledger, with 0 mismatches.
- **Both:**
  - No transaction of the account's was missing from the books.
  - Every one of the account's transactions was signed by the regular key `ravzzNVJTvE8kj13knvY7NJ2ykxPgvRrdm`, never the master key.
  - Every transaction was an OfferCreate or an OfferCancel; there were no transfers.
  - Zero offers were left on the ledger after every stop.
- **Fill hashes:** all 674, with ledger, side, role and size, are in `results/m8_live_report.json` under each period's `fill_hashes`.

## Limitations

- **Short and one-way.** 7.55 h of quoting over three days, in a market that fell 5.5%. That says little about whether the strategy makes money; it says more about how it behaves in a falling market.
- **Two budget periods.** After the breach, period 2 started from what was left, mostly RLUSD, so its first hours couldn't sell. The periods aren't one continuous €50 experiment.
- **Paper's starting balances differ from live's** in period 2, so the inventory parts of the P&L aren't like-for-like. The fill comparison is.
- **Not every difference is explained.** The live against paper differences are attributed in aggregate (refills, races, queue and timing), not fill by fill. The fill-by-fill explanations are deferred until after Ethan's exams (his decision, 10 Nov 2026).
- **One mark from the book.** The monitor was down at session 2's stop, so that mark is the order book mid read from the ledger just after (1.144093, ledger 108204075), not the monitor's reference mid.
- **The euro rate** is the opening rate, 1.1165 USD per EUR, throughout, with RLUSD counted as USD.
- **The report script has no tests of its own.** Its totals are asserted against the books, and the books were checked against the ledger by hash.

## Ideas after M8

Listed by Ethan on 9 Nov 2026, not built (ROADMAP):
- **Widen the margin in fast markets.** Both taker fills came from the price moving more within 2 ledgers than the margin covers.
- **Flatten inventory before stopping.** 1.28 RLUSD, 60% of the loss, came from holding 28.4 XRP overnight while stopped.
