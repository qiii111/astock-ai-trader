#!/usr/bin/env python3
"""Publish quotes for a fixed PUBLIC sample universe; never access private holdings."""
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Public security identifiers only. No Supabase credentials or private portfolio reads.
SYMBOLS = ("003816", "000582", "513180", "159549", "159692", "520920")
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
quotes = {}
errors = []

def market(code):
    return "sh" if code.startswith(("5", "6", "9")) else "sz"

def get_text(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read().decode("gbk", errors="replace")

def eastmoney(code):
    secid = ("1." if market(code) == "sh" else "0.") + code
    url = "https://push2.eastmoney.com/api/qt/stock/get?" + urllib.parse.urlencode(
        {"secid": secid, "fields": "f43,f57,f58,f124"})
    data = json.loads(get_text(url)).get("data") or {}
    price = float(data["f43"]) / 1000  # raw f43 uses three decimal places
    ts = int(data["f124"])
    if price <= 0 or ts < 1500000000:
        raise ValueError("invalid quote")
    return {"price": price, "timestamp": ts}

def tencent(code):
    url = "https://qt.gtimg.cn/q=" + market(code) + code
    payload = get_text(url)
    match = re.search(r'="([^"]+)"', payload)
    if not match:
        raise ValueError("missing quote")
    fields = match.group(1).split("~")
    if len(fields) < 31 or fields[2] != code:
        raise ValueError("unexpected quote")
    price = float(fields[3])
    when = dt.datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
        tzinfo=dt.timezone(dt.timedelta(hours=8)))
    ts = int(when.timestamp())
    if price <= 0 or ts < 1500000000:
        raise ValueError("invalid quote")
    return {"price": price, "timestamp": ts}

for code in SYMBOLS:
    for source, fetch in (("eastmoney", eastmoney), ("tencent", tencent)):
        try:
            quotes[code] = fetch(code)
            break
        except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{code} {source}: {type(exc).__name__}")
            if source == "eastmoney":
                time.sleep(0.3)

out = Path("docs/market_quotes.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "source": "Public per-symbol quotes (Eastmoney / Tencent fallback)",
    "quotes": quotes,
    "errors": errors,
}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("Public symbols:", len(quotes), "fetch errors:", len(errors))
print("Quote coverage:", len(quotes), "/", len(SYMBOLS))
if not quotes:
    print("WARNING: no public quotes fetched; dashboard will display unavailable")
