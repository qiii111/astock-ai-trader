#!/usr/bin/env python3
"""Export public market-wide A-share/ETF quotes; NEVER read private holdings."""
import datetime as dt
import json
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://push2.eastmoney.com/api/qt/clist/get"
FIELDS = "f12,f13,f2,f124"
# Broad PUBLIC universes only. No private holdings, credentials or user watchlists.
UNIVERSES = (
    "m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23",
    "m:0+t:8,m:1+t:8",
    "m:0+t:4,m:1+t:4,m:0+t:5,m:1+t:5",
)
PAGE_SIZE = 100
MAX_PAGES = 100
RETRIES = 3
quotes = {}
errors = []

def fetch_page(universe, page):
    params = {"pn": page, "pz": PAGE_SIZE, "po": 1, "np": 1,
              "fltt": 2, "invt": 2, "fid": "f12", "fs": universe, "fields": FIELDS}
    url = BASE + "?" + urllib.parse.urlencode(params)
    last_error = None
    for attempt in range(RETRIES):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://quote.eastmoney.com/",
                              "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
            data = payload.get("data") or {}
            rows = data.get("diff") or []
            return list(rows.values()) if isinstance(rows, dict) else rows
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                last_error += " HTTP " + str(exc.code)
            if attempt + 1 < RETRIES:
                time.sleep(attempt + 1)
    raise RuntimeError(last_error or "UnknownFetchError")

for universe_index, universe in enumerate(UNIVERSES, 1):
    for page in range(1, MAX_PAGES + 1):
        try:
            rows = fetch_page(universe, page)
        except RuntimeError as exc:
            detail = f"{exc} universe {universe_index} page {page}"
            errors.append(detail)
            print("Quote fetch failed:", detail)
            break
        if not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("f12", ""))
            if len(code) != 6 or not code.isdigit():
                continue
            try:
                price = float(row.get("f2"))
                timestamp = int(row.get("f124"))
            except (ValueError, TypeError):
                continue
            if price <= 0 or timestamp < 1500000000 or timestamp > 4102444800:
                continue
            quotes[code] = {"price": price, "timestamp": timestamp}
        if len(rows) < PAGE_SIZE:
            break

out = Path("docs/market_quotes.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "source": "Eastmoney public market list",
    "quotes": quotes,
    "errors": errors,
}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("Public symbols:", len(quotes), "fetch errors:", len(errors))
if not quotes:
    print("WARNING: no public quotes fetched; dashboard will display unavailable")
