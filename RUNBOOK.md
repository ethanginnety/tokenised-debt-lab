# Runbook

## Start and stop

```bash
source .venv/bin/activate
python -m debtlab.api            # starts on http://127.0.0.1:8000
# stop: Ctrl+C (records an app_stopped event)
```

Configuration comes from environment variables. See `.env.example`. Copy it to `.env` and export the values, or set them inline.

## Health

```bash
curl http://127.0.0.1:8000/api/health
```

- `status: ok` means the database answers.
- `curves` shows each curve's status: `fresh`, `stale` or `illustrative`.
- `last_refresh` is when prices were last recomputed.

## First online run (do this once, carefully)

1. Start with `DEBTLAB_DATA_MODE=online python -m debtlab.api`.
2. Open the dashboard and check the **Where the rates come from** table. Each curve's observation date should be the latest business day, and its status `fresh`.
3. Spot-check two rates against the source websites:
   - US Treasury: the 10-year on the Treasury "Daily Treasury Par Yield Curve Rates" page.
   - ECB: the 10-year AAA spot rate on the ECB Data Portal.
4. If anything differs, stop and record it in `CHANGELOG.md`. The parsers haven't yet been verified against real downloads.
5. Expect the century bond to show **"not priced"** in online mode: the gilt adapter is milestone M2. That's intended. The app never substitutes made-up data.

## When a data source is down

The app records a `marketdata_error` event and keeps using the last stored official curve. That curve's age keeps growing, and after 96 hours it turns `stale` and the risk gate blocks any risk-taking. Nothing needs doing unless the outage lasts. Check the audit log, filtered to `marketdata_error`.

## Restart and recovery

All state is in the SQLite file (`data/debtlab.sqlite3` by default). Restarting reruns migrations (safely, as a no-op), reloads curves and revalues. Repeated valuations on the same data and date are not duplicated.

To start completely fresh, stop the app and delete the `data/` folder. You lose the audit history.

## Share a point-in-time view

```bash
python scripts/export_snapshot.py snapshot.html --full
```

This writes a single HTML file with today's numbers baked in.

## Emergency stop

Milestone 1 places no orders, so stopping the process is enough. A persisted kill switch arrives with the market maker (M4) and must be tested before any live trading (M8).
