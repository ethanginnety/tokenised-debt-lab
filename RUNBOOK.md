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

## Main and scenario databases

The app's normal database (`data/debtlab.sqlite3`) is marked **main**. Lifecycle demos (advancing the simulation date, triggering the default) run only in a separate database marked **scenario**, so the main one keeps a performing short bond for the market maker. The dashboard header says which one it is reading.

```powershell
python scripts/new_scenario_db.py                       # creates data/scenario.sqlite3 with the test holders
$env:DEBTLAB_DB_PATH = "data/scenario.sqlite3"; python -m debtlab.api
Remove-Item Env:DEBTLAB_DB_PATH                         # back to the main database
```

### Coupons, maturity and default in the scenario database

The scenario starts on the day it was created, and its simulation date only moves forward. Run these with the app stopped or running (SQLite allows both):

```powershell
python scripts/lifecycle.py status              # simulation date, bond status, payments, amounts owed
python scripts/lifecycle.py pay                 # make every payment due by the simulation date
python scripts/lifecycle.py advance 2027-03-30  # move the date forward, then pay what is due
python scripts/lifecycle.py advance 2029-02-20  # past the short bond's maturity: principal paid, status matured
```

The default scenario instead (start from a fresh scenario database; maturity and default are alternatives):

```powershell
python scripts/lifecycle.py pay                          # the August 2026 coupon, paid normally
python scripts/lifecycle.py default SHORT-USD-2029       # the next coupon (17 Feb 2027) will be missed
python scripts/lifecycle.py advance 2027-02-17           # coupon missed, grace period, priced flat at 1,000 bp
python scripts/lifecycle.py advance 2027-03-19           # default declared, priced at the recovery value, quoting halted
python scripts/lifecycle.py advance 2028-03-20           # 40.90 per 100 recovered, units retired, losses per holder
```

With the app running on the scenario database, the dashboard's *Lifecycle scenario* section shows the result of each command: reload the page (or press Refresh) after running one. The page and `GET /api/lifecycle` only read; there is no button that moves the date or triggers a default. A step shown as *due* means its date has passed but `pay` hasn't run yet.

`pay` is always safe to repeat: if a run stops part-way, running it again pays only the holders not yet paid. Dana's amounts show as *unpaid (payments blocked)*. Add `--db PATH` for another scenario database; the script refuses a main database. A scenario database made before 30 Oct's coupon step has no simulation date: recreate it. One from the coupon step picks up the maturity table automatically.

Run `new_scenario_db.py` again to start the demo from scratch. Stop the app first (Windows locks open files). The previous scenario database is moved to `data/archive/`, never deleted. The script refuses to replace any database that isn't marked as a scenario.

## Share a point-in-time view

```bash
python scripts/export_snapshot.py snapshot.html --full
```

This writes a single HTML file with today's numbers baked in.

## Emergency stop

Milestone 1 places no orders, so stopping the process is enough. A persisted kill switch arrives with the market maker (M4) and must be tested before any live trading (M8).

## Multi-day runs on Windows (M7 monitor and paper run)

The RLUSD/XRP monitor and the paper run (M7) need the machine awake and online for days. Until M9 nothing restarts them for you. Before a run:

1. **Plug in and stop sleep.** Settings → System → Power & battery → Screen, sleep & hibernate timeouts: *When plugged in, put my device to sleep after* **Never** (the screen may turn off; that's fine). Or in a terminal: `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`. On a laptop, Control Panel → Power Options → *Choose what closing the lid does* → **Do nothing** when plugged in. The monitor also asks Windows not to sleep while it runs, **but that doesn't stop standby on this laptop, so these settings are required.** On 1-2 Nov all 10 gaps over 30 s began as the laptop entered standby, six on closing the lid and four on an idle timeout (three of those while plugged in). Leave it plugged in: the "plugged in" settings do nothing on battery.
2. **Keep Windows Update from restarting the machine.** Settings → Windows Update → *Pause updates* for the length of the run. Install anything pending and restart *before* starting. Set *Active hours* to cover as much of the day as allowed, as a second guard.
3. **Keep the network up.** Prefer a cable. Device Manager → Network adapters → your adapter → Power Management: untick *Allow the computer to turn off this device to save power*.
4. **Keep the database out of OneDrive's way.** This repository lives in a OneDrive folder, and syncing a SQLite database while it is being written can lock or conflict. Point the monitor's `--db` at a folder outside OneDrive, such as `C:\debtlab-data\xrpl.sqlite3` (recommended), or pause OneDrive syncing for the run (it resumes by itself after at most 24 hours, so renew it).
5. **Don't hide gaps.** Whatever stops the run (sleep, update restart, crash, lost Wi-Fi), the monitor records it: disconnects and stale data as outages while it runs, and time it wasn't running at all as a gap found from its last heartbeat on the next start. Uptime is reported with every gap. Restart it as soon as you notice; never edit or delete outage records.
6. After the run, undo these settings if you want normal sleep and updates back.

## M8 mainnet: Ethan's setup

**Real funds.** Plan approved by Ethan on 7 Nov 2026 (ROADMAP, *M8 mainnet plan*). Claude never touches funds, never sees a seed or key, and never sends a transfer: only tell Claude **public addresses** (they start with `r`). **The account's master key never touches the laptop:** it stays in Xaman. Nothing here places an order; the first order still needs your explicit go, after the checklist below. Commands run from the repository with the virtual environment active.

Xaman's menus change between versions, so the names below may differ slightly. If a step isn't where it says, stop and tell Claude what you see. Don't use a website that asks for your secret.

1. **The account.** Use your existing dedicated Xaman account, holding about €60 of XRP and used for nothing else. Tell Claude its address. Claude reads it back read-only (balance, trust lines, offers, objects, regular key) and says whether anything is unexpected.
2. **RLUSD trust line.** In Xaman, add the token **RLUSD**, issuer **`rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De`**, checking the issuer character by character: a look-alike, `rLUSDtyk…`, uses the same currency code (XRPL_VERIFICATION.md). Set the limit to **100** if Xaman asks. If it doesn't let you choose, add it anyway: `check` (step 7) shows the limit, and the bot won't start on a limit over 100 until you and Claude agree what to do. It costs 0.2 XRP of reserve.
3. **Split XRP and RLUSD.** Swap about €25 of XRP into RLUSD in Xaman yourself, using its swap or DEX. Leave at least 2 XRP of reserve untouched. Claude reads the balances back.
4. **Make the bot's key, on the laptop.** This creates `C:\debtlab-secrets\xrpl-bot.key`, locks the folder so only your Windows account can open it, writes a brand-new key straight into the file and prints **only its address**:
   ```
   python scripts/xrpl_mainnet.py keygen
   ```
   It refuses to overwrite an existing key, or to write one inside the repository or OneDrive. **Never open, copy, back up or paste the file.** If it's ever exposed, remove the regular key in Xaman (step 5) and run `keygen` again after deleting the file yourself. The bot's code signs only offers and cancels, but anyone else holding the key could sign anything for the account, payments included: that's why it stays in the locked file, and why you can remove it with the master key.
5. **Set it as the regular key, in Xaman.** In the account's settings (Settings > Accounts > the account > Advanced, or similar), choose **Set regular key**, paste the address printed in step 4, check it character by character, and sign with Xaman. **Don't disable the master key:** you need it to remove the bot's key. The bot refuses to start if the ledger's regular key isn't exactly this key, and it never accepts the master key.
6. **Tell Claude the two addresses:** the account's, and the bot key's from step 4. Claude puts the account in `config/xrpl_mainnet.json` (a reviewed change), reads the regular key back from the ledger, and restarts nothing without you.
7. **Restart the mainnet monitor with the bot's account left out,** so the bot never prices from its own offers. Stop the monitor's window with Ctrl+C, then:
   ```
   python scripts/xrpl_monitor.py run --db C:\debtlab-data\xrpl.sqlite3 --exclude-account rACCOUNT
   ```
8. **Check everything; nothing is sent:**
   ```
   python scripts/xrpl_mainnet.py check --key-file C:\debtlab-secrets\xrpl-bot.key
   ```
   It opens the key with every check, then checks the account: the regular key, exactly one trust line (RLUSD from the verified issuer, limit no higher than `trust_line_limit_rlusd` in the config, not frozen), no other ledger objects (a signer list, escrow, check, ticket or payment channel all refuse), no offer the bot didn't place, a total worth at most €70, and no freeze by the issuer. It prints the limits and the hard stop, then `all checks passed` or each problem.
9. **Shadow run, about 30 minutes; nothing is sent.** It reads and reconciles the real account on every ledger, on its own database (`xrpl_mainnet_shadow.sqlite3`), and every send is refused before it leaves, even after a `resume`:
   ```
   python scripts/xrpl_mainnet.py run --shadow --key-file C:\debtlab-secrets\xrpl-bot.key --for 1800
   ```
   Then show that `kill` leaves zero offers, and reset it:
   ```
   python scripts/xrpl_mainnet.py kill --key-file C:\debtlab-secrets\xrpl-bot.key --reason "checklist: kill leaves zero offers"
   python scripts/xrpl_mainnet.py reset --reason "checklist done"
   ```
10. **Stop and send Claude the outputs** of steps 8 and 9. Claude ticks the checklist below with the evidence.

### Checklist before Ethan's go (each recorded with its evidence)

| Item | Evidence |
|---|---|
| The full test suite and the mainnet safety tests pass | `python -m pytest tests -q`, run by Claude on the commit you'll run |
| The account matches: regular key, one trust line, balances about €25 a side, nothing else | Claude's read-only read, and `check` |
| Limits loaded: order €10 (quotes about €9), daily loss €5, total €50, data age 30 s, 2 open orders; offers live 60 s at most | `check` prints them |
| The shadow run is clean, and `kill` leaves zero offers | step 9's output, and the account on a mainnet explorer |
| The monitor is fresh and leaves the bot's account out | `check` refuses otherwise |
| Paper alongside will run on its own database, `xrpl_mainnet_paper.sqlite3` | `run` opens it; the M7 database stays untouched |
| The laptop is plugged in, with the lid open | you |
| No secret-shaped string in any database or log, and the key file readable only by you | Claude's scan of the databases and outputs; `check` refuses a readable key |
| The issuer's clawback and freeze (R16) and the Irish tax record (R13) reviewed | RISK_REGISTER.md, with you |
| The hard stop is Wednesday 11 Nov 2026 20:00 Irish time (20:00 UTC) | `check` and `status` print it |
| **Your explicit go** | in the chat |

## M8 mainnet run: Ethan's steps (after the checklist and your go)

**Real funds.** Supervised sessions, plugged in, lid open, the first about an hour. Up to 24 h of uptime in all, or until the hard stop, whichever comes first (`results/m8_live_cutoff.txt`).

1. **Start the bot,** in its own window. It starts paused and places nothing yet:
   ```
   python scripts/xrpl_mainnet.py run --key-file C:\debtlab-secrets\xrpl-bot.key
   ```
2. **The first order comes from this one,** run by you in another window:
   ```
   python scripts/xrpl_mainnet.py resume --reason "first mainnet orders"
   ```
   Quotes appear after about two minutes: the strategy needs 30 price changes first. The bot uses €50 of the account and sets the rest aside: `status` shows both.
3. **Watch it:** `python scripts/xrpl_mainnet.py status`, and `compare` for live against paper, fill by fill.
4. **It pauses itself,** cancelling everything, on: the first fill that takes liquidity, any disagreement with the ledger, money moved from outside, or the €5 daily loss. Send Claude the output; don't resume until you've talked it through.
5. **Stop it at any time:**
   - `kill --key-file C:\debtlab-secrets\xrpl-bot.key --reason WHY` cancels from the ledger's own list until none remain, with the bot running or not, and stays on until `reset --reason WHY`. If it can't reach the network, each offer still expires by itself within 60 s.
   - `pause --reason WHY`: a running bot cancels on its next tick.
   - Ctrl+C in the bot's window cancels everything and stops. Starting it again leaves it paused.
   - At the hard stop the bot stops itself, cancelling everything; `run` and `resume` refuse after it.
6. **After each session,** tell Claude it's stopped. Claude records the session's uptime, as for paper.
7. **Afterwards, to take the key away:** remove the regular key in Xaman, then delete `C:\debtlab-secrets\xrpl-bot.key` yourself.

## M8 testnet run: Ethan's steps

Test funds with no value, on testnet only. Do steps 1-4 yourself; Claude never sees or opens a key. **Nothing is ordered until you start it at step 7, after your own go.** Don't use any testnet secret for anything else, and never put a mainnet secret in these files. Commands run from the repository with the virtual environment active. Keep the laptop plugged in.

1. **Make the bot's testnet account.** On the [XRP Ledger faucets page](https://xrpl.org/resources/dev-tools/xrp-faucets), choose **Testnet** and generate credentials. It shows an address (starts with `r`) and a secret (starts with `s`).
2. **Put the secret in the key file.** Create the folder `C:\debtlab-secrets`. In Notepad, paste only the secret and save it as `C:\debtlab-secrets\xrpl-testnet.key` (check it didn't become `.key.txt`). Then lock it in PowerShell:
   ```
   icacls C:\debtlab-secrets\xrpl-testnet.key /inheritance:r /grant:r "${env:USERNAME}:R"
   ```
   The bot refuses a key file inside the repository or OneDrive, or one other accounts can read, and tells you which.
3. **Trust line and test RLUSD.** In Xaman: turn on developer mode and switch to **XRPL Testnet**, then under Settings > Accounts add an account by importing the **family seed** from step 1, and check the `r` address matches. Add a trust line for **RLUSD**, issuer **`rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV`**, checked character by character. Get test RLUSD from [tryrlusd.com](https://tryrlusd.com). It doesn't matter if the account holds more than €50 worth: the bot only uses €50 of it (step 7).
4. **A second account, for the crossing helper.** Repeat steps 1-3 and save its secret as `C:\debtlab-secrets\xrpl-testnet-taker.key`, locked the same way.
5. **Start the testnet monitor,** in its own window. It doesn't touch the mainnet monitor or its data:
   ```
   python scripts/xrpl_monitor.py run --network testnet --db C:\debtlab-data\xrpl_testnet.sqlite3 --exclude-account rBOT
   ```
6. **Tell Claude the two `r` addresses, nothing else,** so the balances can be read back.
7. **After your go, start the bot,** in another window. It starts paused and places nothing yet. It refuses to start if the account also exists on mainnet.
   ```
   python scripts/xrpl_testnet.py --db C:\debtlab-data\xrpl_live.sqlite3 --paper-db C:\debtlab-data\xrpl_testnet_paper.sqlite3 --monitor-db C:\debtlab-data\xrpl_testnet.sqlite3 run --key-file C:\debtlab-secrets\xrpl-testnet.key
   ```
   All the commands below take the same three `--db` options before the command name. **The first order comes from this one:**
   ```
   python scripts/xrpl_testnet.py ... resume --reason "first testnet orders"
   ```
   Quotes appear after about two minutes: the strategy needs 30 price changes first. If the account is worth more than €50, the bot uses €50 of it, in the account's own mix of XRP and RLUSD, and sets the rest aside: `status` shows both. Raising the limit later doesn't release what was set aside.
8. **Watch it:** `status`, and `compare` for live against paper.
9. **Stop it at any time:**
   - `kill --key-file C:\debtlab-secrets\xrpl-testnet.key --reason WHY` cancels from the ledger's own list until none remain, with the bot running or not, and stays on until `reset --reason WHY`. It doesn't need mainnet to answer: starting the bot does, but cancelling doesn't. If it can't reach testnet, each offer still expires by itself within 60 s.
   - `pause --reason WHY`: a running bot cancels on its next tick.
   - Ctrl+C in the bot's window cancels everything and stops. Starting it again leaves it paused.

**What the run has to show (ROADMAP, acceptance for step 8):**

| Check | How |
|---|---|
| A real fill, booked and compared | `python tests/live_testnet/taker.py --key-file C:\debtlab-secrets\xrpl-testnet-taker.key --bot-account rBOT --side sell --size 5`. It reads the bot's live bid itself, retries a miss, and says whether the fill hit the bot, another offer or the AMM. Then `status` and `compare`. |
| A fill that takes liquidity stops it | Not forced: the margin should prevent it. Covered by tests, and the bot pauses if it ever happens. |
| Kill leaves zero offers | `kill`, then check the address on a testnet explorer. |
| Offers stay live | Read back over the run: a live bid in at least 95% of ledgers while the bot is quoting. Claude measures it from the ledger. |
| Stale data | With the bot quoting, stop the testnet monitor. After 30 s the bot's window says `stopped quoting: data …`, it cancels and the state stays `running`. Start the monitor again and it says `quoting again`, with no `resume`. |
| Dropped connection | Turn Wi-Fi off, wait a minute, turn it on. Offers expire by themselves meanwhile. (The bot's own reads and sends move between `testnet.xrpl-labs.com` and `clio.altnet.rippletest.net` if only one is lost. The monitor uses only the first, so losing that one still stops quoting.) |
| Order limit | `limit max_order_eur 1 --reason test` refuses orders. Put it back with `limit max_order_eur 10`. |
| Open-order limit | `limit max_open_orders 1 --reason test` lets only one side rest. Put it back with `limit max_open_orders 2`. |
| Total limit | `limit max_total_eur 1 --reason test` refuses orders. Put it back with `limit max_total_eur 50`. |
| Daily loss | `limit max_daily_loss_eur 0.001 --reason test` (not 0: fees take the day's P&L below zero by themselves, which fires it by accident), then `python tests/live_testnet/taker.py --key-file C:\debtlab-secrets\xrpl-testnet-taker.key --bot-account rBOT --side sell --size 1 --rest --price PRICE --minutes 10`, with Claude working out PRICE from the book. The offer pulls the mid down, the day's loss passes the cap, and the bot pauses, printing it once. Put the limit back with `limit max_daily_loss_eur 5` **before** `resume`. |
| Restart cleans up | Close the bot's window without Ctrl+C, start it again, and check it's paused with nothing left. |

## RLUSD/XRP monitor (M7, read-only)

```bash
python scripts/xrpl_monitor.py run --db C:\debtlab-data\xrpl.sqlite3     # until Ctrl+C
python scripts/xrpl_monitor.py status --db C:\debtlab-data\xrpl.sqlite3  # in another window, any time
python scripts/xrpl_monitor.py status --hours 24                          # just the last day
```

Read-only: it holds no key and can't place anything. Set `DEBTLAB_XRPL_DB` once instead of passing `--db` every time (keep the file outside OneDrive; see *Multi-day runs on Windows*). It prints a line every 50 ledgers (about 3 minutes) and a warning if the RLUSD issuer's settings change. If it loses the connection it records an outage and retries, switching endpoints. If it stops (crash, sleep, update restart), start it again: the gap is recorded as *not running*, and uptime in `status` includes it. Expect about 0.75 GB a day of download and about 50 MB a day of database.

The dashboard's *RLUSD/XRP* section shows the same figures, refreshed every 10 s. Start the app with the same `DEBTLAB_XRPL_DB` as the monitor (or neither, for the default `data/xrpl.sqlite3`); the app only reads that file. On Windows, in its own PowerShell window, which stays open:

```powershell
cd "C:\Users\ethan\OneDrive\Documents\tokenised-debt-lab\tokenised-debt-lab"
$env:DEBTLAB_XRPL_DB="C:\debtlab-data\xrpl.sqlite3"
$env:DEBTLAB_DATA_MODE="offline"
$env:DEBTLAB_PORT="8001"
.\.venv\Scripts\python.exe -m debtlab.api
```

Then open http://127.0.0.1:8001. The `$env:` values last only for that window, so set them again each time.

*Couldn't load the RLUSD/XRP monitor: Failed to fetch* means **the app** has stopped, not the monitor: a page left open keeps asking every 10 s and gets no answer. Start the app again and the section recovers by itself within 10 s. A fault in `/api/xrpl` itself would show `HTTP 500` instead.

For the step 4 checkpoint, run it until `status` shows at least **48 hours of actual uptime** (Ethan, 2 Nov 2026: hours up, not hours on the clock), then send Claude the output of `status`. Checkpoint moved by Ethan to the evening of Wednesday 4 Nov 2026 (it was due after 22:50 on 3 Nov) because of the gaps listed in ROADMAP. **Accepted by Ethan on 5 Nov 2026 on 41.33 h of uptime, short of 48 h, because decision C passes by wide margins (his decision).**

## RLUSD/XRP paper bot (M7, PAPER; run only after Ethan's go)

```bash
python scripts/xrpl_paper.py status                          # any time
python scripts/xrpl_paper.py resume --reason "start paper run"   # it starts paused
python scripts/xrpl_paper.py run                             # until Ctrl+C; the monitor must be running
python scripts/xrpl_paper.py pause  --reason WHY             # cancels every paper order
python scripts/xrpl_paper.py kill   --reason WHY             # same, and stays on across restarts
python scripts/xrpl_paper.py reset  --reason WHY             # kill switch off; leaves it paused
python scripts/xrpl_paper.py limit max_order_eur 8 --reason WHY
```

PAPER: no key, no wallet, nothing reaches a ledger. It reads the monitor's database (`DEBTLAB_XRPL_DB`, or `--monitor-db`) and writes its own (`DEBTLAB_XRPL_PAPER_DB`, default `data/xrpl_paper.sqlite3`); keep both outside OneDrive, e.g. `C:\debtlab-data\`. Start the monitor first and leave it running: the paper bot never restarts or disturbs it. If the monitor stops or the data is older than 30 s, the paper bot cancels everything and waits. If the paper bot stops, start it again: it cancels whatever the last run left open, never uses trades from while it was down, and records the gap. `status` shows both fill rules side by side; the truth is between them.

The strategy's values were frozen by Ethan on 5 Nov 2026 (`config/xrpl.json` `paper.strategy`, see ASSUMPTIONS). They expect a negative edge. No retuning on paper P&L, during or after the run.

**Run it as supervised sessions until they add up to 24 h of uptime (Ethan, 5 Nov 2026; decision F, given the 14 Nov deadline).** The unclean reboots all happened in standby, so don't leave a run unattended:

1. Before each session: plugged in and lid open. On battery, closing the lid sleeps the laptop and it also sleeps after 45 idle minutes.
2. Start the monitor, then the paper bot. Quoting needs about two minutes of history after a monitor restart.
3. Stay with it. If the laptop has to move or close, stop first.
4. End with Ctrl+C in the paper bot's window, then in the monitor's.
5. Record the session in `results/m7_paper_sessions.txt`: start and end times (UTC), and the output of both `status` commands. Gaps between sessions stay in the record; never remove them.

Each session, from the repository with the virtual environment active, with the mainnet monitor already running:

```
python scripts/xrpl_paper.py --db C:\debtlab-data\xrpl_paper.sqlite3 --monitor-db C:\debtlab-data\xrpl.sqlite3 run
python scripts/xrpl_paper.py --db C:\debtlab-data\xrpl_paper.sqlite3 --monitor-db C:\debtlab-data\xrpl.sqlite3 resume --reason "paper session N"
python scripts/xrpl_paper.py --db C:\debtlab-data\xrpl_paper.sqlite3 --monitor-db C:\debtlab-data\xrpl.sqlite3 status
```

`run` stays open in its own window and starts paused, every session, so each one needs its own `resume`. Paper `status` doesn't show uptime: Claude works it out after each session from the recorded decisions, counting time with a quote and no gap over 30 s, as the monitor counts its own. The sessions end when those hours reach 24.

The end-of-run report, read-only, for the first 24 h of uptime (what came after is reported separately):

```
python scripts/xrpl_paper_report.py --db C:\debtlab-data\xrpl_paper.sqlite3 --monitor-db C:\debtlab-data\xrpl.sqlite3
```

It writes `results/m7_paper_report.md` and `.json`.
