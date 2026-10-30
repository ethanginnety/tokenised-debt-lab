"""Diagnostic: download the Bank of England yield-curve data and show its layout."""
import io
import re
import zipfile

import httpx
import openpyxl

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
PAGE = "https://www.bankofengland.co.uk/statistics/yield-curves"
GUESS = "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip"

print("== links to data files on the yield-curves page ==")
try:
    page = httpx.get(PAGE, headers=UA, timeout=30, follow_redirects=True)
    print("page HTTP", page.status_code)
    for link in sorted(set(re.findall(r'href="([^"]+\.(?:zip|xlsx))"', page.text, re.I))):
        print("  ", link)
except Exception as exc:
    print("page FAILED:", type(exc).__name__, exc)

print("\n== trying the latest-data zip ==")
try:
    r = httpx.get(GUESS, headers=UA, timeout=60, follow_redirects=True)
    print("HTTP", r.status_code, "|", len(r.content), "bytes |", r.headers.get("content-type"))
    z = zipfile.ZipFile(io.BytesIO(r.content))
    for name in z.namelist():
        print("\nFILE:", name)
        if not name.lower().endswith(".xlsx"):
            continue
        wb = openpyxl.load_workbook(io.BytesIO(z.read(name)), read_only=True, data_only=True)
        print("  sheets:", wb.sheetnames)
        for sheet in wb.sheetnames:
            if "spot" not in sheet.lower():
                continue
            ws = wb[sheet]
            print(f"  -- first rows of '{sheet}' (first 8 columns) --")
            rows = list(ws.iter_rows(values_only=True))
            for row in rows[:6]:
                print("    ", [c for c in row[:8]])
            print(f"  -- last row of '{sheet}' (first 8 and last 4 columns) --")
            last = [row for row in rows if row and row[0] is not None][-1]
            print("    ", list(last[:8]), "...", list(last[-4:]), "| columns:", len(last))
except Exception as exc:
    print("zip FAILED:", type(exc).__name__, exc)