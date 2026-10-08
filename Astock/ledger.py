#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持久化共享账本（仅标准库）。

关键设计：**幂等**。定时任务可能因重试/重叠触发而重复执行，
因此每次"成交"都必须绑定唯一 trade_key，落库时 UNIQUE 约束保证
同一笔决策只会成交一次，重复执行直接跳过（不重复扣款/买入）。

同时提供 run_registry 记录每次定时执行，用于：
  - 去重（同一 run_key 已完成则跳过）
  - 可核验的执行记录（状态、时间、结果摘要）
"""
import os
import sqlite3
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "astock.db")

ACCOUNTS = {
    "real22000": {"initial": 22000.0, "label": "实盘对标（2.2万）"},
    "paper100k": {"initial": 100000.0, "label": "对照账户（10万）"},
}


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 并发安全
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_ledger() -> None:
    conn = connect()
    # 旧库迁移：旧 positions 表无 account 列，与新版（多账户）不兼容 -> 重命名归档
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(positions)")]
    if cols and "account" not in cols:
        conn.execute("ALTER TABLE positions RENAME TO positions_legacy")
        conn.commit()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS acct(
            account TEXT PRIMARY KEY,
            initial REAL,
            cash REAL,
            label TEXT
        );
        CREATE TABLE IF NOT EXISTS positions(
            account TEXT, code TEXT, name TEXT,
            shares INTEGER, avg_cost REAL, buy_date TEXT,
            PRIMARY KEY(account, code)
        );
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT, trade_key TEXT UNIQUE,
            date TEXT, code TEXT, name TEXT, action TEXT,
            price REAL, shares INTEGER, amount REAL,
            commission REAL, stamp_tax REAL, transfer_fee REAL, cost_total REAL,
            realized_pnl REAL, reason TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS equity_curve(
            account TEXT, date TEXT, cash REAL, market_value REAL,
            total REAL, pct_change REAL, market_date TEXT,
            PRIMARY KEY(account, date)
        );
        CREATE TABLE IF NOT EXISTS run_registry(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT, task_name TEXT, status TEXT,
            account TEXT, started_at TEXT, finished_at TEXT,
            market_date TEXT, source TEXT, note TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_acct ON trades(account, date);
        CREATE INDEX IF NOT EXISTS idx_equity_acct ON equity_curve(account, date);
        """
    )
    # 向前兼容：为旧库补充 error 列（问题 #6：失败必须记录明确原因）
    try:
        conn.execute("ALTER TABLE run_registry ADD COLUMN error TEXT")
    except sqlite3.OperationalError:
        pass
    for acct, meta in ACCOUNTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO acct(account, initial, cash, label) VALUES(?,?,?,?)",
            (acct, meta["initial"], meta["initial"], meta["label"]),
        )
    conn.commit()
    conn.close()


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_already_done(conn, run_key: str) -> bool:
    row = conn.execute(
        "SELECT status FROM run_registry WHERE run_key=? AND status IN ('done','running') "
        "ORDER BY id DESC LIMIT 1", (run_key,),
    ).fetchone()
    return row is not None


def start_run(conn, run_key: str, task_name: str, account: str) -> None:
    conn.execute(
        """INSERT INTO run_registry(run_key,task_name,status,account,started_at)
           VALUES(?,?,?,?,?)""",
        (run_key, task_name, "running", account, now()),
    )
    conn.commit()


def finish_run(conn, run_key: str, status: str, market_date: str, source: str, note: str) -> None:
    conn.execute(
        """UPDATE run_registry SET status=?, finished_at=?, market_date=?, source=?, note=?
           WHERE run_key=? AND status='running'""",
        (status, now(), market_date, source, note, run_key),
    )
    conn.commit()


def trade_exists(conn, trade_key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM trades WHERE trade_key=? LIMIT 1", (trade_key,)
    ).fetchone() is not None


def record_trade(conn, row: dict) -> bool:
    """幂等写入成交。trade_key 冲突则跳过，返回 False（表示重复）。"""
    if trade_exists(conn, row["trade_key"]):
        return False
    cols = ",".join(row.keys())
    ph = ",".join("?" for _ in row)
    conn.execute(f"INSERT INTO trades({cols}) VALUES({ph})", tuple(row.values()))
    conn.commit()
    return True


def get_cash(conn, account: str) -> float:
    r = conn.execute("SELECT cash FROM acct WHERE account=?", (account,)).fetchone()
    return float(r["cash"]) if r else 0.0


def set_cash(conn, account: str, cash: float) -> None:
    conn.execute("UPDATE acct SET cash=? WHERE account=?", (cash, account))
    conn.commit()


def upsert_position(conn, account: str, code: str, name: str,
                    shares: int, avg_cost: float, buy_date: str) -> None:
    if shares <= 0:
        conn.execute("DELETE FROM positions WHERE account=? AND code=?", (account, code))
    else:
        conn.execute(
            """INSERT INTO positions(account,code,name,shares,avg_cost,buy_date)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(account,code) DO UPDATE SET
                 name=excluded.name, shares=excluded.shares,
                 avg_cost=excluded.avg_cost, buy_date=excluded.buy_date""",
            (account, code, name, shares, avg_cost, buy_date),
        )
    conn.commit()


def get_positions(conn, account: str):
    return conn.execute(
        "SELECT * FROM positions WHERE account=? ORDER BY code", (account,)
    ).fetchall()


def upsert_equity(conn, account: str, date: str, cash: float,
                  market_value: float, market_date: str) -> None:
    total = cash + market_value
    row = conn.execute(
        "SELECT total FROM equity_curve WHERE account=? AND date=? ORDER BY date DESC LIMIT 1",
        (account, date),
    ).fetchone()
    conn.execute(
        """INSERT OR REPLACE INTO equity_curve(account,date,cash,market_value,total,pct_change,market_date)
           VALUES(?,?,?,?,?,?,?)""",
        (account, date, cash, market_value, total, 0.0, market_date),
    )
    conn.commit()
