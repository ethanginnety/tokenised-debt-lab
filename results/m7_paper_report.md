# PAPER: M7 paper run report, RLUSD/XRP on mainnet data

**PAPER: no order was placed on any ledger. Fills are inferred from real mainnet trades under two rules; the truth is somewhere between the conservative and optimistic accounts, and neither models the queue ahead of us, so read fills as an upper bound.**

Generated from the paper and monitor databases by `scripts/xrpl_paper_report.py` (read-only). Times are UTC. Euro figures convert RLUSD at 1 USD (decision D) and the ECB rate at the end of each window.

## Window

- **Reported result:** the first 24 h of uptime (decision F), from the accounts' opening 2026-11-05 14:15:25 to 2026-11-07 01:42:09: 35.45 h of wall-clock time, 24.000 h of uptime. The cutoff was fixed by Ethan before the results were looked at.
- **Separate check, not part of the result:** 2026-11-07 01:42:09 to 2026-11-07 10:16:22, 8.462 h of uptime.
- Uptime is time with a quote and no gap over 30 s between quotes. Mid (reference): 1.302505 at opening, 1.179654 at the cutoff (-9.43%), 1.169674 at the end. EUR/USD 1.1267 at opening, 1.1165 at the cutoff.

## Headline (first 24 h of uptime)

| EUR | conservative | optimistic |
|---|---:|---:|
| Start value | 50.0000 | 50.0000 |
| End value | 48.9974 | 48.9974 |
| **Total P&L** | -1.4594 | -1.4594 |
| Spread captured | 3.0562 | 3.0562 |
| Network fees | -0.1481 | -0.1481 |
| Inventory, while quoting | -1.9581 | -1.9581 |
| Inventory, while not quoting | -2.4094 | -2.4094 |
| Doing nothing (held the opening balances) | -2.3795 | -2.3795 |
| **Bot versus doing nothing** | 0.9201 | 0.9201 |
| Largest drawdown | -2.8283 | -2.8283 |

Total P&L = spread + fees + inventory exactly (checked when the report is built). Start value is EUR 50 at the opening rate; end value is at the cutoff's rate, so the two differ by the EUR/USD move as well as the P&L. The P&L lines are in RLUSD converted at the cutoff's rate.

## Detail: first 24 h of uptime (the result)

| | conservative | optimistic |
|---|---:|---:|
| Start XRP / RLUSD | 21.625636 / 28.167500 | 21.625636 / 28.167500 |
| End XRP / RLUSD | 20.092187 / 31.003763 | 20.092187 / 31.003764 |
| Start / end value, RLUSD | 56.3350 / 54.7056 | 56.3350 / 54.7056 |
| Orders placed | 7,053 | 7,053 |
| Filled in full | 803 | 803 |
| Cancelled (of which part-filled) | 6,248 (898) | 6,248 (898) |
| Fills | 2,429 | 2,430 |
| XRP bought / sold | 4,295.18 / 4,296.57 | 4,295.18 / 4,296.57 |
| Fee transactions / XRP | 13,786 / 0.137860 | 13,786 / 0.137860 |
| Spread / fees, RLUSD | 3.4123 / -0.1654 | 3.4123 / -0.1654 |
| Inventory quoting / not, RLUSD | -2.1862 / -2.6901 | -2.1862 / -2.6901 |
| Total P&L, RLUSD / EUR | -1.6294 / -1.4594 | -1.6294 / -1.4594 |
| Doing nothing, RLUSD | -2.6567 | -2.6567 |
| Largest drawdown, RLUSD | 3.1578 (2026-11-05 14:30:42 to 2026-11-05 23:17:56) | 3.1578 (2026-11-05 14:30:42 to 2026-11-05 23:17:56) |
| Markout 60 s, RLUSD (fills) | 1.7047 (2,429) | 1.7047 (2,430) |
| Markout 300 s, RLUSD (fills) | 1.5322 (2,429) | 1.5322 (2,430) |

By UTC day, RLUSD:

| Day | Uptime h | conservative: fills / spread / fees / inventory / total | optimistic: fills / spread / fees / inventory / total |
|---|---:|---:|---:|
| 2026-11-05 | 3.25 | 455 / 0.5747 / -0.0285 / -3.3558 / -2.8096 | 456 / 0.5747 / -0.0285 / -3.3558 / -2.8096 |
| 2026-11-06 | 19.08 | 1,851 / 2.7475 / -0.1335 / -1.5262 / 1.0878 | 1,851 / 2.7475 / -0.1335 / -1.5262 / 1.0878 |
| 2026-11-07 | 1.67 | 123 / 0.0900 / -0.0034 / 0.0057 / 0.0924 | 123 / 0.0900 / -0.0034 / 0.0057 / 0.0924 |

Limit events: 0 pauses by the risk check; 38 orders refused.

| Refused orders, by reason | Count |
|---|---:|
| conservative: not enough RLUSD on the conservative account: room for # XRP after open orders, the reserve and fees | 15 |
| optimistic: not enough RLUSD on the optimistic account: room for # XRP after open orders, the reserve and fees | 15 |
| conservative: not enough XRP on the conservative account: room for # XRP after open orders, the reserve and fees | 3 |
| optimistic: not enough XRP on the optimistic account: room for # XRP after open orders, the reserve and fees | 3 |
| conservative: not enough XRP for the reserve and fees on the conservative account: # held, # needed | 1 |
| optimistic: not enough XRP for the reserve and fees on the optimistic account: # held, # needed | 1 |

| No quote, by reason | Decisions |
|---|---:|
| only # mid changes in the last # min; need # | 60 |
| book invalid: prices crossed: bid # >= offer # | 25 |
| bot is paused (restarted: quoting needs a resume) | 16 |
| connection: monitor outage open (disconnected: wss://xrplcluster.com/: ConnectionError: no close frame receive | 3 |

Monitor outages overlapping the window: 86, stale 129.0 min, disconnected 538.6 min, missed_ledgers 7.6 min.

## Separate check: after the cutoff

| | conservative | optimistic |
|---|---:|---:|
| Start XRP / RLUSD | 20.092187 / 31.003763 | 20.092187 / 31.003764 |
| End XRP / RLUSD | 27.405763 / 22.902475 | 27.405763 / 22.902475 |
| Start / end value, RLUSD | 54.7056 / 54.9583 | 54.7056 / 54.9583 |
| Orders placed | 1,637 | 1,637 |
| Filled in full | 254 | 254 |
| Cancelled (of which part-filled) | 1,385 (309) | 1,385 (309) |
| Fills | 877 | 877 |
| XRP bought / sold | 1,376.02 / 1,368.67 | 1,376.02 / 1,368.67 |
| Fee transactions / XRP | 3,171 / 0.031710 | 3,171 / 0.031710 |
| Spread / fees, RLUSD | 1.0277 / -0.0370 | 1.0277 / -0.0370 |
| Inventory quoting / not, RLUSD | -0.7595 / 0.0214 | -0.7595 / 0.0214 |
| Total P&L, RLUSD / EUR | 0.2527 / 0.2263 | 0.2527 / 0.2263 |
| Doing nothing, RLUSD | -0.2005 | -0.2005 |
| Largest drawdown, RLUSD | 0.4822 (2026-11-07 01:43:13 to 2026-11-07 04:14:13) | 0.4822 (2026-11-07 01:43:13 to 2026-11-07 04:14:13) |
| Markout 60 s, RLUSD (fills) | 0.6348 (877) | 0.6348 (877) |
| Markout 300 s, RLUSD (fills) | 0.6370 (877) | 0.6370 (877) |

By UTC day, RLUSD:

| Day | Uptime h | conservative: fills / spread / fees / inventory / total | optimistic: fills / spread / fees / inventory / total |
|---|---:|---:|---:|
| 2026-11-07 | 8.46 | 877 / 1.0277 / -0.0370 / -0.7381 / 0.2527 | 877 / 1.0277 / -0.0370 / -0.7381 / 0.2527 |

Limit events: 0 pauses by the risk check; 8 orders refused.

| Refused orders, by reason | Count |
|---|---:|
| conservative: not enough RLUSD on the conservative account: room for # XRP after open orders, the reserve and fees | 4 |
| optimistic: not enough RLUSD on the optimistic account: room for # XRP after open orders, the reserve and fees | 4 |

| No quote, by reason | Decisions |
|---|---:|
| book invalid: prices crossed: bid # >= offer # | 4 |

Monitor outages overlapping the window: 15, missed_ledgers 0.8 min, disconnected 3.2 min, stale 3.1 min.

## Sessions and gaps

| Run | Started | Stopped | First quote | Last quote | Uptime h | In the result |
|---|---|---|---|---|---:|---:|
| 1 | 2026-11-05 14:14:50 | 2026-11-05 14:37:03 | 2026-11-05 14:15:25 | 2026-11-05 14:37:02 | 0.360 | 0.360 |
| 2 | 2026-11-05 21:03:23 | 2026-11-06 15:54:54 | 2026-11-05 21:03:24 | 2026-11-06 15:54:50 | 14.224 | 14.224 |
| 3 | 2026-11-06 16:02:53 | 2026-11-07 10:16:22 | 2026-11-06 16:04:06 | 2026-11-07 10:16:20 | 17.878 | 9.416 |

| Gap from | to | Minutes | Window |
|---|---|---:|---|
| 2026-11-05 14:37:02 | 2026-11-05 21:03:24 | 386.4 | main |
| 2026-11-05 23:11:07 | 2026-11-05 23:14:15 | 3.1 | main |
| 2026-11-06 01:08:12 | 2026-11-06 01:10:32 | 2.3 | main |
| 2026-11-06 03:59:52 | 2026-11-06 04:00:22 | 0.5 | main |
| 2026-11-06 04:00:22 | 2026-11-06 04:01:10 | 0.8 | main |
| 2026-11-06 05:39:15 | 2026-11-06 05:40:06 | 0.8 | main |
| 2026-11-06 05:53:31 | 2026-11-06 05:55:56 | 2.4 | main |
| 2026-11-06 07:58:17 | 2026-11-06 08:00:30 | 2.2 | main |
| 2026-11-06 09:49:26 | 2026-11-06 09:54:08 | 4.7 | main |
| 2026-11-06 09:59:04 | 2026-11-06 10:19:06 | 20.0 | main |
| 2026-11-06 11:39:26 | 2026-11-06 15:40:28 | 241.0 | main |
| 2026-11-06 15:54:50 | 2026-11-06 16:04:06 | 9.3 | main |
| 2026-11-06 18:37:50 | 2026-11-06 18:39:55 | 2.1 | main |
| 2026-11-06 19:04:05 | 2026-11-06 19:06:15 | 2.2 | main |
| 2026-11-06 19:24:42 | 2026-11-06 19:26:52 | 2.2 | main |
| 2026-11-06 20:28:59 | 2026-11-06 20:31:20 | 2.4 | main |
| 2026-11-06 23:42:19 | 2026-11-06 23:44:28 | 2.1 | main |
| 2026-11-07 00:10:09 | 2026-11-07 00:12:19 | 2.2 | main |
| 2026-11-07 04:06:14 | 2026-11-07 04:08:27 | 2.2 | after |
| 2026-11-07 04:43:17 | 2026-11-07 04:45:24 | 2.1 | after |
| 2026-11-07 07:33:27 | 2026-11-07 07:35:34 | 2.1 | after |

Limits in force (never changed during the run): max_order_eur 10, max_daily_loss_eur 5, max_total_eur 50, max_data_age_seconds 30, max_open_orders 2.

