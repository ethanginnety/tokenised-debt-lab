"""The live RLUSD/XRP monitor (M7 step 3). READ-ONLY: no keys, no wallet, no orders.

Driven by scripts/xrpl_monitor.py. On every validated ledger it reads both sides of the
book (shallow; deep every N ledgers) and the AMM pool *at that ledger* and stores a
snapshot; trades come from validated transaction metadata; outages are recorded with
their reasons; endpoints fail over with backoff.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets

from debtlab.xrpl_market import book, fx, store, trades, verify
from debtlab.xrpl_market.readonly import XrplReadError
from debtlab.xrpl_market.wsclient import ReadOnlyWebsocket


def now() -> datetime:
    return datetime.now(timezone.utc)


RIPPLE_EPOCH = 946684800  # seconds from 1970 to 2000-01-01, the ledger's epoch


class KeepAwake:
    """Ask Windows not to sleep while the monitor runs (no effect elsewhere)."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, *exc):
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)


class Monitor:
    def __init__(self, conn, cfg: dict, *, until: datetime | None = None, save_raw: Path | None = None,
                 out=print, fetch_fx=fx.fetch_ecb_eurusd, exclude: str | None = None):
        self.conn, self.cfg, self.until, self.out = conn, cfg, until, out
        self.exclude = exclude
        self.fetch_fx = fetch_fx
        self.m = cfg["monitor"]
        r = cfg["rlusd"]
        self.rlusd_hex, self.issuer = r["currency_hex"], r["issuer"]
        self.rlusd = {"currency": self.rlusd_hex, "issuer": self.issuer}
        self.xrp = {"currency": "XRP"}
        self.save_raw, self.saved = save_raw, 0
        self.run_id: int | None = None
        self.amm_account: str | None = None
        self.last_fx: datetime | None = None
        self.last_thin = now()
        self.ledgers = 0
        self.trades = 0

    def done(self) -> bool:
        return self.until is not None and now() >= self.until

    async def run(self) -> None:
        self.run_id = store.start_run(self.conn, now())
        self.out(f"Monitor run {self.run_id} started {now():%Y-%m-%d %H:%M:%S} UTC. READ-ONLY. Ctrl+C to stop.")
        await self.refresh_fx()
        urls, i, backoff = self.cfg["endpoints"]["websocket"], 0, 1.0
        reason = "stopped"
        try:
            while not self.done():
                url = urls[i % len(urls)]
                try:
                    async with ReadOnlyWebsocket(url) as ws:
                        await self.session(ws, url)
                        backoff = 1.0
                except (OSError, XrplReadError, ConnectionError, asyncio.TimeoutError,
                        websockets.WebSocketException) as e:
                    t = now()
                    store.open_outage(self.conn, "disconnected", t, f"{url}: {type(e).__name__}: {e}")
                    i += 1
                    store.event(self.conn, t, "failover", f"{url} failed ({e}); next {urls[i % len(urls)]} in {backoff:.0f}s")
                    self.out(f"{t:%H:%M:%S} {url} failed: {e}. Retrying {urls[i % len(urls)]} in {backoff:.0f}s")
                    await self.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
            reason = "finished (--for)"
        except (KeyboardInterrupt, asyncio.CancelledError):
            reason = "stopped by user"
            raise
        finally:
            store.stop_run(self.conn, self.run_id, now(), reason)
            self.out(f"Stopped ({reason}): {self.ledgers} ledgers read, {self.trades} trades recorded.")

    async def sleep(self, seconds: float) -> None:
        end = now() + timedelta(seconds=seconds)
        while now() < end and not self.done():
            store.heartbeat(self.conn, self.run_id, now())
            await asyncio.sleep(min(2.0, (end - now()).total_seconds()))

    async def session(self, ws: ReadOnlyWebsocket, url: str) -> None:
        sub = await ws.command("subscribe", streams=["ledger"],
                               books=[{"taker_gets": self.rlusd, "taker_pays": self.xrp, "both": True}])
        first = int(sub["ledger_index"])
        pool = await ws.command("amm_info", asset=self.xrp, asset2=self.rlusd, ledger_index=first)
        self.amm_account = pool["amm"]["account"]
        await ws.command("subscribe", accounts=[self.amm_account])
        await self.check_issuer(ws, first)
        store.event(self.conn, now(), "connected", f"{url} at ledger {first}; AMM {self.amm_account}")
        self.out(f"{now():%H:%M:%S} connected to {url} at ledger {first}")
        last_data = now()
        last_beat = now()
        while not self.done():
            try:
                msg = await asyncio.wait_for(ws.stream.get(), timeout=min(5.0, store.FRESH_SECONDS / 3))
            except asyncio.TimeoutError:
                msg = None
            t = now()
            quiet = (t - last_data).total_seconds()
            if quiet > store.FRESH_SECONDS:
                store.open_outage(self.conn, "stale", last_data, f"no validated ledger read for over "
                                  f"{store.FRESH_SECONDS}s from {url}")
            if quiet > 2 * store.FRESH_SECONDS:
                raise ConnectionError(f"no validated ledger for {quiet:.0f}s; reconnecting")
            if (t - last_beat).total_seconds() >= 10:
                store.heartbeat(self.conn, self.run_id, t)
                last_beat = t
            if msg is None:
                continue
            typ = msg.get("type")
            if typ == "_closed":
                raise ConnectionError(msg.get("reason", "connection closed"))
            if typ == "ledgerClosed":
                if await self.on_ledger(ws, url, msg):
                    last_data = now()
            elif typ == "transaction":
                self.on_transaction(msg)

    async def on_ledger(self, ws, url, msg) -> bool:
        li = int(msg["ledger_index"])
        close = datetime.fromtimestamp(int(msg["ledger_time"]) + RIPPLE_EPOCH, timezone.utc)
        deep = li % self.m["deep_every_ledgers"] == 0
        limit = self.m["deep_limit"] if deep else self.m["top_limit"]
        try:
            bids, asks, pool = await asyncio.gather(
                ws.command("book_offers", taker_gets=self.rlusd, taker_pays=self.xrp, limit=limit, ledger_index=li),
                ws.command("book_offers", taker_gets=self.xrp, taker_pays=self.rlusd, limit=limit, ledger_index=li),
                ws.command("amm_info", asset=self.xrp, asset2=self.rlusd, ledger_index=li))
        except XrplReadError as e:
            store.event(self.conn, now(), "read_failed", f"ledger {li}: {e}")
            return False
        b = book.parse_book([bids], [asks], self.rlusd_hex, page_limit=limit)
        st = store.record_snapshot(self.conn, self.run_id, url, li, close, now(), "deep" if deep else "top", b,
                                   verify.amm(pool, self.rlusd_hex), self.exclude)
        self.ledgers += 1
        if li % self.m["issuer_every_ledgers"] == 0:
            await self.check_issuer(ws, li)
        if deep:
            line = (f"bid {st.best_bid:.6f} ask {st.best_ask:.6f} spread {st.spread_bp:.2f} bp"
                    if st.valid else "INVALID: " + "; ".join(st.reasons))
            self.out(f"{now():%H:%M:%S} ledger {li} {line} | {self.ledgers} ledgers, {self.trades} trades")
        if self.last_fx is None or now() - self.last_fx > timedelta(hours=self.m["fx_every_hours"]):
            await self.refresh_fx()
        if now() - self.last_thin > timedelta(hours=1):
            store.thin(self.conn, now())
            self.last_thin = now()
        return True

    def on_transaction(self, msg: dict) -> None:
        try:
            found = trades.extract(msg, self.rlusd_hex, self.issuer, self.amm_account)
        except trades.NotValidated:
            return
        except (ValueError, KeyError) as e:
            store.event(self.conn, now(), "tx_unreadable", f"{type(e).__name__}: {e}")
            return
        if found:
            self.trades += store.record_trades(self.conn, found, now())
            if self.save_raw and self.saved < self.m["save_raw_max"]:
                self.save_raw.mkdir(parents=True, exist_ok=True)
                h = found[0].tx_hash
                (self.save_raw / f"tx_{h}.json").write_text(json.dumps(msg, indent=1), encoding="utf-8", newline="")
                self.saved += 1

    async def check_issuer(self, ws, li: int) -> None:
        try:
            info = await ws.command("account_info", account=self.issuer, ledger_index=li)
            state = await ws.command("server_state")
        except XrplReadError as e:
            store.event(self.conn, now(), "read_failed", f"issuer/network check at {li}: {e}")
            return
        if store.record_issuer(self.conn, verify.issuer_settings(info), now()):
            self.out(f"WARNING: the RLUSD issuer's settings changed at ledger {li}; see xrpl_events")
        store.record_network(self.conn, verify.network_costs(state), now())

    async def refresh_fx(self) -> None:
        try:
            rate, _ = await asyncio.to_thread(self.fetch_fx)
            store.record_fx(self.conn, rate, now())
            self.out(f"ECB EUR/USD {rate.usd_per_eur} ({rate.obs_date})")
        except fx.FxError as e:
            store.event(self.conn, now(), "fx_failed", str(e))
            self.out(f"ECB EUR/USD fetch failed: {e} (using the last stored rate, if any)")
        self.last_fx = now()


def status(conn, hours: float | None, out=print) -> None:
    t = now()
    out(f"RLUSD/XRP monitor, LIVE MARKET DATA, READ-ONLY. Now {t:%Y-%m-%d %H:%M:%S} UTC")
    runs = conn.execute("SELECT * FROM xrpl_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not runs:
        out("Never run.")
        return
    state = "running" if runs["stopped_at"] is None else f"stopped {runs['stopped_at']} ({runs['stop_reason']})"
    out(f"Latest run {runs['id']}: started {runs['started_at']}, {state}")
    last = conn.execute("SELECT * FROM xrpl_snapshots ORDER BY ledger_index DESC LIMIT 1").fetchone()
    if last:
        age = (t - store.parse(last["received_at"])).total_seconds()
        fresh = "fresh" if age <= store.FRESH_SECONDS else "STALE"
        line = (f"bid {last['best_bid'][:8]} ask {last['best_ask'][:8]} spread {float(last['spread_bp']):.2f} bp"
                if last["valid"] else "INVALID " + last["reasons"])
        out(f"Latest snapshot: ledger {last['ledger_index']}, {age:.0f}s old ({fresh}); {line}")
        if last["amm_xrp"]:
            out(f"  AMM pool {float(last['amm_xrp']):,.0f} XRP + {float(last['amm_rlusd']):,.0f} RLUSD, fee {last['amm_fee_pct']}%")
    since = t - timedelta(hours=hours) if hours else None
    u = store.uptime(conn, t, since)
    if u.share is not None:
        out(f"Uptime since {u.since:%Y-%m-%d %H:%M} UTC: {u.share:.2%} ({u.up_s / 3600:.2f} of {u.span_s / 3600:.2f} h)")
    rate = store.latest_fx(conn)
    if rate:
        out(f"ECB EUR/USD {rate.usd_per_eur} on {rate.obs_date}{' (STALE)' if rate.stale(t) else ''}; 1 RLUSD taken as 1 USD")
    c = store.depth_check(conn, since)
    out(f"Decision C over {c.ledgers_read} ledgers ({c.deep_reads} deep reads, {c.deep_reads_complete} reaching 50 bp):")
    out(f"  both sides funded {c.both_sides_share:.2%}" if c.both_sides_share is not None else "  no ledgers yet")
    if c.median_spread_bp is not None:
        out(f"  median spread {c.median_spread_bp:.2f} bp")
    if c.median_bid_eur_50bp is not None:
        out(f"  median depth within 50 bp: bids EUR {c.median_bid_eur_50bp:,.0f}, asks EUR {c.median_ask_eur_50bp:,.0f}")
    for k, v in c.passes.items():
        out(f"  [{'pass' if v else 'FAIL' if v is False else ' -- '}] {k}")
    q = "SELECT venue, taker_side, COUNT(*) n, SUM(CAST(rlusd AS REAL)) r FROM xrpl_trades"
    args = []
    if since:
        q += " WHERE received_at >= ?"
        args.append(store.iso(since))
    q += " GROUP BY venue, taker_side ORDER BY venue, taker_side"
    rows = conn.execute(q, args).fetchall()
    out("Trades: " + ("; ".join(f"{r['venue']} {r['taker_side']} {r['n']} ({r['r']:,.0f} RLUSD)" for r in rows) or "none"))
    outs = conn.execute("SELECT * FROM xrpl_outages ORDER BY id DESC LIMIT 10").fetchall()
    out(f"Outages (latest {len(outs)}):" if outs else "Outages: none")
    for o in outs:
        out(f"  {o['kind']:<15} {o['started_at']} -> {o['ended_at'] or 'OPEN'}  {o['detail']}")
    ev = conn.execute("SELECT * FROM xrpl_events WHERE kind NOT IN ('connected') ORDER BY id DESC LIMIT 5").fetchall()
    for e in ev:
        out(f"  event {e['at']} {e['kind']}: {e['detail']}")
