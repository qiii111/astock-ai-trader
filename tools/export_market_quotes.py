#!/usr/bin/env python3
"""Export public market-wide A-share/ETF quotes; NEVER read private holdings."""
import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://push2.eastmoney.com/api/qt/clist/get"
FIELDS = "f12,f13,f2,f124"
# Broad public universes, not a private user's portfolio/watchlist.
UNIVERSES = ("m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23",
             "m:0+t:8,m:1+t:8",
             "m:0+t:4,m:1+t:4,m:0+t:5,m:1+t:5")
quotes = {}
errors = []
for universe in UNIVERSES:
    page = 1
    while page <= 80:
        params = {"pn":page,"pz":1000,"po":1,"np":1,"fltt":2,"invt":2,
                  "fid":"f12","fs":universe,"fields":FIELDS}
        url = BASE + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req,timeout=18) as response:
                data = json.load(response).get("data") or {}
            rows = data.get("diff") or []
            if isinstance(rows,dict): rows=list(rows.values())
            if not rows: break
            for row in rows:
                code=str(row.get("f12",""))
                price=row.get("f2")
                ts=row.get("f124")
                if len(code)!=6 or not code.isdigit():continue
                try:
                    price=float(price);ts=int(ts)
                except (ValueError,TypeError):continue
                if price<=0 or ts<1500000000 or ts>4102444800:continue
                quotes[code]={"price":price,"timestamp":ts}
            if len(rows)<1000:break
            page+=1
        except Exception as exc:
            errors.append(type(exc).__name__+" page "+str(page))
            break
out=Path("docs/market_quotes.json")
out.parent.mkdir(parents=True,exist_ok=True)
# Empty output is explicit and must not silently become a fake quote snapshot.
out.write_text(json.dumps({"generated_at":dt.datetime.now(dt.timezone.utc).isoformat(),
 "source":"Eastmoney public market list","quotes":quotes,"errors":errors},ensure_ascii=False,separators=(",",":")),encoding="utf-8")
print("Public symbols:",len(quotes),"fetch errors:",len(errors))
if not quotes:print("WARNING: no public quotes fetched; dashboard will display unavailable")
