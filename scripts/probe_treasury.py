"""Diagnostic: try four ways of downloading the US Treasury curve and report which work.

    python scripts/probe_treasury.py
"""
import time

import httpx

BASE = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
URLS = {
    "whole year": BASE + "/2026/all?type=daily_treasury_yield_curve&field_tdr_date_value=2026&page&_format=csv",
    "one month": BASE + "/all/202610?type=daily_treasury_yield_curve&field_tdr_date_value_month=202610&page&_format=csv",
}
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

for name, url in URLS.items():
    for label, headers in (("plain", {}), ("browser-like", BROWSER)):
        start = time.time()
        try:
            r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
            lines = r.text.splitlines()
            print(f"{name:10} | {label:12} | HTTP {r.status_code} | {time.time() - start:5.1f}s | {len(lines)} lines")
            print("    first line:", lines[0][:90] if lines else "(empty)")
            print("    latest row:", lines[1][:90] if len(lines) > 1 else "(none)")
        except Exception as exc:
            print(f"{name:10} | {label:12} | FAILED after {time.time() - start:5.1f}s: {type(exc).__name__}: {exc}")
