#!/usr/bin/env python3
"""Export public simulated-account data, never credentials."""
import datetime,json,os,sys
sys.path.insert(0,os.path.join(os.path.dirname(__file__),"..","Astock"))
import pg_ledger as P
conn=P.connect()
try:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT account,initial,cash,label FROM acct ORDER BY account")
        accounts=[]
        for a in cur.fetchall():
            key=a["account"]
            cur.execute("SELECT code,name,shares,avg_cost FROM positions WHERE account=%s ORDER BY code",(key,))
            positions=cur.fetchall()
            cur.execute("SELECT date,action,name,shares,price FROM trades WHERE account=%s ORDER BY id DESC LIMIT 20",(key,))
            trades=cur.fetchall()
            cur.execute("SELECT date,total,market_value FROM equity_curve WHERE account=%s ORDER BY date DESC LIMIT 90",(key,))
            equity=list(reversed(cur.fetchall()))
            last=equity[-1] if equity else None
            initial=float(a["initial"])
            total=float(last["total"]) if last else float(a["cash"])
            accounts.append({"account":key,"label":a["label"],"cash":a["cash"],"initial":initial,
              "total":total,"market_value":float(last["market_value"]) if last else 0,
              "return_pct":round((total/initial-1)*100,2) if initial else 0,
              "valuation_date":last["date"] if last else None,
              "positions":positions,"trades":trades})
    conn.rollback()
finally:
    conn.close()
os.makedirs("docs",exist_ok=True)
with open("docs/data.json","w",encoding="utf-8") as f:
    json.dump({"generated_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "accounts":accounts},f,ensure_ascii=False,default=str)
print("Public simulated-account snapshot created; no secrets included.")
