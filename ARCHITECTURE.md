# Architecture

One application, one database, no microservices. Pieces are kept separate in code so each can be tested alone.

```
 config/instruments.json         official sources (daily)      illustrative curves
          │                       Treasury · ECB · BoE(M2)      (offline only)
          ▼                                 │                         │
   instruments.py                  marketdata/adapters.py ◄───────────┘
          │                                 │  parse → CurveSnapshot (source, date, kind)
          │                                 ▼
          │                       audit/db.py  curve_snapshots (last valid survives outages)
          ▼                                 │
   pricing/ (pure functions) ◄──── service.py ────► audit/db.py  events (append-only)
   dates · curve · bond · engine            │
                                            ▼
                                     api/app.py (FastAPI)
                                     /api/health  /api/overview  /api/lifecycle  /api/events  /api/refresh
                                            │
                                            ▼
                                     api/static/index.html (dashboard)
```

## Components

| Component | Now (M1) | Later |
|---|---|---|
| Pricing engine | `debtlab/pricing`: pure Decimal functions, no I/O. | Curve bootstrap (M2). |
| Market data | Official adapters (Treasury, ECB), illustrative curves, staleness and risk gate. | BoE gilts (M2), RLUSD/XRP book (M7). |
| Fund share (C) | `instruments.py` `FundShare`, `NavReport`; `nav_reports` table; `service.fund_view`. NAV, age and staleness; never quoted. | Withdrawal windows, queue, gates, secondary price (M5). |
| Simulated ledger | `ledger.py`: parties (holders, issuers, market maker, market, funding), double-entry journal (`ledger_txns`, `ledger_entries`), balances as of a date, transfers, trades with a cash leg (`trade`: clean + accrued, flat in grace or default), seeding from `config/holders.json`, final record dates in `bond_terms`. `scenario.py` makes scenario databases; `db_meta` marks each database main or scenario. | |
| Lifecycle | `lifecycle.py` (scenario databases only): a stored simulation date that only moves forward; coupons scheduled from holdings at the record date (`payment_events`, `entitlements`), then paid one holder per atomic block. Principal at maturity the same way, units returned to the issuer in the holder's transaction; `instrument_status` records *matured*. Default scenario: `trigger_default` stores the terms (`defaults` table); `pay_due` then records the next coupon *missed*, declares default after the grace period and pays the recovery like principal; `scenario_price` prices each state (flat after the missed coupon). `scripts/lifecycle.py` drives it. `service.lifecycle_view` reads it for `/api/lifecycle` and the dashboard, read-only, in one read transaction. | Controls from the dashboard, if ever (a separate decision). |
| Market maker | `marketmaker/book.py`: the parties and starting book from `config/marketmaker.json`, seeded in main (and in a scenario database created with `--market-maker`). `marketmaker/quotes.py`: pure quote model, valuation + inventory → bid/ask with every spread component and its reason. | Limits, simulated market, P&L (M4 steps 3–7); strategy and risk interfaces shared by simulation, paper and live adapters (M7, M8). |
| XRPL integration | n/a | `xrpl-py`; testnet tokens (M6), mainnet read-only, paper, then gated live (M7, M8). |
| Database | SQLite with numbered migrations; `events`, `curve_snapshots`, `nav_reports`, `db_meta`, `parties`, `ledger_txns`, `ledger_entries`, `payment_events`, `entitlements`, `instrument_status`, `defaults`, `bond_terms`. Main and scenario databases share the schema. A migration whose first line is `-- foreign-keys: off` rebuilds a referenced table with foreign keys off, and every reference is checked before it commits. | PostgreSQL for deployment (M9). |
| API and dashboard | FastAPI; plain HTML, JS and SVG. | Controls (pause, kill switch) in M4. |
| Worker and scheduler | Curves load on start and on Refresh. | Scheduled refresh, heartbeats, alerts (M9). |

## Design rules

- **Decimal everywhere** for prices, rates and cash. Rounding happens only for display and storage, using banker's rounding.
- **UTC timestamps.** Every curve carries its source, observation date, fetch time and kind (`official_daily` or `illustrative`).
- **Never invent data.** A failed fetch falls back to the last stored *official* curve, flagged by its age, or leaves the instrument unpriced. Online mode never uses illustrative curves.
- **Risk gate.** `risk_increasing_allowed()` refuses illustrative, missing or stale data. Every future quoting or trading path must pass it.
- **Double entry.** Every ledger transaction sums to zero per asset, is written all or nothing, and has a deterministic id: posting it again does nothing, and the same id with different contents is refused. Balances are sums of entries, never stored.
- **Main vs scenario.** Lifecycle demos run only in a database marked `scenario`; the main database is never advanced. `require_scenario()` guards every lifecycle action.
- **Payments are idempotent per holder.** Entitlements are fixed when a payment is scheduled. Each holder's ledger transaction (id `coupon:<bond>:<pay date>:<holder>`) and its entitlement's *paid* mark commit together, and a run only touches entitlements still *due*, so a run cut short is finished by running it again. Scheduling closes the books: the ledger refuses unit movements dated on or before a scheduled record date, and once principal is scheduled, any movement in that bond but a redemption. Units can't move after a bond's final record date except to be redeemed.
- **Idempotent events.** Valuations carry a key (instrument, curve snapshot, settlement date), so repeated runs record once.
- **Two pricing views.** Curve pricing gives fair value. Yield pricing (street convention, matching QuantLib) gives yield, duration and convexity. Effective duration from curve bumps is shown too, because the two durations differ slightly by definition.
