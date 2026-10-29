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
                                     /api/health  /api/overview  /api/events  /api/refresh
                                            │
                                            ▼
                                     api/static/index.html (dashboard)
```

## Components

| Component | Now (M1) | Later |
|---|---|---|
| Pricing engine | `debtlab/pricing`: pure Decimal functions, no I/O. | Curve bootstrap (M2). |
| Market data | Official adapters (Treasury, ECB), illustrative curves, staleness and risk gate. | BoE gilts (M2), RLUSD/XRP book (M7). |
| Simulated ledger | n/a | Holder balances, coupons, maturity, default (M3). |
| Market maker | n/a | Strategy and risk interfaces shared by simulation, paper and live adapters (M4, M7, M8). |
| XRPL integration | n/a | `xrpl-py`; testnet tokens (M6), mainnet read-only, paper, then gated live (M7, M8). |
| Database | SQLite with numbered migrations; `events` and `curve_snapshots`. | PostgreSQL for deployment (M9). |
| API and dashboard | FastAPI; plain HTML, JS and SVG. | Controls (pause, kill switch) in M4. |
| Worker and scheduler | Curves load on start and on Refresh. | Scheduled refresh, heartbeats, alerts (M9). |

## Design rules

- **Decimal everywhere** for prices, rates and cash. Rounding happens only for display and storage, using banker's rounding.
- **UTC timestamps.** Every curve carries its source, observation date, fetch time and kind (`official_daily` or `illustrative`).
- **Never invent data.** A failed fetch falls back to the last stored *official* curve, flagged by its age, or leaves the instrument unpriced. Online mode never uses illustrative curves.
- **Risk gate.** `risk_increasing_allowed()` refuses illustrative, missing or stale data. Every future quoting or trading path must pass it.
- **Idempotent events.** Valuations carry a key (instrument, curve snapshot, settlement date), so repeated runs record once.
- **Two pricing views.** Curve pricing gives fair value. Yield pricing (street convention, matching QuantLib) gives yield, duration and convexity. Effective duration from curve bumps is shown too, because the two durations differ slightly by definition.
