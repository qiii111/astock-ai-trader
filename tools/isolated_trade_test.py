#!/usr/bin/env python3
"""Isolated execution tests: all mutations target session-local TEMP tables."""
import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Astock"))
import pg_ledger as P
import run_cloud as R
import daily_job as J


def verify():
    conn = P.connect()
    try:
        with conn.cursor() as c:
            # Temporary tables shadow production names. No production table or
            # sequence is written; temp tables vanish when the session closes.
            c.execute("""CREATE TEMP TABLE acct (
                account text PRIMARY KEY, initial double precision,
                cash double precision, label text) ON COMMIT PRESERVE ROWS""")
            c.execute("""CREATE TEMP TABLE positions (
                account text, code text, name text, shares integer,
                avg_cost double precision, buy_date text,
                PRIMARY KEY(account,code)) ON COMMIT PRESERVE ROWS""")
            c.execute("""CREATE TEMP TABLE trades (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                account text, trade_key text UNIQUE, date text, code text,
                name text, action text, price double precision, shares integer,
                amount double precision, commission double precision,
                stamp_tax double precision, transfer_fee double precision,
                cost_total double precision, realized_pnl double precision,
                reason text, created_at text) ON COMMIT PRESERVE ROWS""")
            c.execute("INSERT INTO acct VALUES ('isolated_test', 100000, 100000, 'temporary')")
            c.execute("SET search_path TO pg_temp, public")
        conn.commit()

        acct = "isolated_test"
        code, name, price = "600000", "Synthetic Test Stock", 10.0
        day = "2026-10-08"
        pm = {code: price}
        st, detail = R.execute_pg(conn, acct, code, name, "BUY", price, 0, day,
                                   "synthetic", price_map=pm)
        assert st == "done", (st, detail)
        pos = P.get_positions(conn, acct)
        assert len(pos) == 1 and pos[0]["shares"] % 100 == 0
        shares = pos[0]["shares"]
        amount, commission, stamp, transfer, cost = J.fees(price, shares, "BUY")
        assert abs(P.get_cash(conn, acct) - (100000 - amount - cost)) < 0.01
        assert commission >= J.COMMISSION_MIN and stamp == 0
        print("PASS: buy, lot size, cash and buy fees")

        st, _ = R.execute_pg(conn, acct, code, name, "BUY", price, 0, day,
                              "synthetic", price_map=pm)
        assert st in ("duplicate", "skip")
        assert len(P.get_positions(conn, acct)) == 1
        print("PASS: duplicate buy cannot double-charge")

        st, _ = R.execute_pg(conn, acct, code, name, "SELL", price, 0, day,
                              "synthetic", price_map=pm)
        assert st == "skip"
        print("PASS: T+1 blocks same-day sell")

        next_day = "2026-10-09"
        st, detail = R.execute_pg(conn, acct, code, name, "SELL", price, 0,
                                   next_day, "synthetic", price_map=pm)
        assert st == "done", (st, detail)
        assert not P.get_positions(conn, acct)
        assert P.get_cash(conn, acct) < 100000
        with conn.cursor() as c:
            c.execute("SELECT action, stamp_tax FROM trades ORDER BY id")
            rows = c.fetchall()
        assert len(rows) == 2 and rows[0]["action"] == "BUY"
        assert rows[1]["action"] == "SELL" and rows[1]["stamp_tax"] > 0
        print("PASS: next-day sell, stamp tax, position cleared")

        cash = P.get_cash(conn, acct)
        st, _ = R.execute_pg(conn, acct, code, name, "SELL", price, 0,
                              next_day, "synthetic", price_map=pm)
        assert st in ("duplicate", "skip") and P.get_cash(conn, acct) == cash
        print("PASS: duplicate sell cannot double-credit")
        print("ISOLATED_EXECUTION_TEST_OK (temporary tables only)")
    finally:
        conn.close()


if __name__ == "__main__":
    verify()
